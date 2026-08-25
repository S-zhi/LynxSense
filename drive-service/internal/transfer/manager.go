// Package transfer runs the two long-lived pipelines exposed by the sidecar:
// local staging to Drive, and Drive download to the existing Python API.
package transfer

import (
	"context"
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	driveclient "github.com/s-zhi/subtitles-ai-drive/internal/drive"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
)

const (
	KindDriveUpload   = "DRIVE_UPLOAD"
	KindPythonImport  = "PYTHON_IMPORT"
	StatePending      = "PENDING"
	StateTransferring = "TRANSFERRING"
	StateRetrying     = "RETRYING"
	StateVerifying    = "VERIFYING"
	StatePaused       = "PAUSED"
	StateSuccess      = "SUCCESS"
	StateFailed       = "FAILED"
	StateCancelled    = "CANCELLED"
)

// Manager owns background transfer workers and their cancellation functions.
// Progress is written to Store before each externally visible step.
type Manager struct {
	cfg   *config.Config
	store *store.Store
	drive *driveclient.Client

	mu      sync.Mutex
	cancels map[string]context.CancelFunc
}

// NewManager constructs the process-wide transfer coordinator.
func NewManager(cfg *config.Config, state *store.Store, client *driveclient.Client) *Manager {
	return &Manager{cfg: cfg, store: state, drive: client, cancels: map[string]context.CancelFunc{}}
}

// StartDriveUpload schedules a resumable local-file to Drive transfer.
func (m *Manager) StartDriveUpload(upload store.Upload) (store.Transfer, error) {
	if !upload.Completed || upload.Length <= 0 || upload.Path == "" {
		return store.Transfer{}, errors.New("upload staging file is incomplete")
	}
	t, err := m.store.CreateTransfer(store.Transfer{
		Kind:       KindDriveUpload,
		State:      StatePending,
		UploadID:   upload.ID,
		FileName:   upload.Name,
		MIME:       upload.MIME,
		LocalPath:  upload.Path,
		TotalBytes: upload.Length,
	})
	if err != nil {
		return store.Transfer{}, err
	}
	m.start(t.ID)
	return t, nil
}

// StartPythonImport schedules a Drive-to-Python import transfer.
func (m *Manager) StartPythonImport(fileID string) (store.Transfer, error) {
	if fileID == "" {
		return store.Transfer{}, errors.New("fileId 不能为空")
	}
	t, err := m.store.CreateTransfer(store.Transfer{
		Kind:   KindPythonImport,
		State:  StatePending,
		FileID: fileID,
	})
	if err != nil {
		return store.Transfer{}, err
	}
	m.start(t.ID)
	return t, nil
}

// Recover restarts transfers left in a non-terminal state after a process exit.
func (m *Manager) Recover() {
	for _, t := range m.store.RecoverableTransfers() {
		m.start(t.ID)
	}
}

// Pause marks a transfer paused and cancels its current HTTP request.
func (m *Manager) Pause(id string) error {
	t, ok := m.store.GetTransfer(id)
	if !ok {
		return os.ErrNotExist
	}
	if t.State == StateSuccess || t.State == StateCancelled {
		return fmt.Errorf("transfer is already %s", t.State)
	}
	if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StatePaused; return nil }); err != nil {
		return err
	}
	m.cancel(id)
	return nil
}

// Resume restarts a paused or failed transfer using its persisted checkpoint.
func (m *Manager) Resume(id string) error {
	t, ok := m.store.GetTransfer(id)
	if !ok {
		return os.ErrNotExist
	}
	if t.State != StatePaused && t.State != StateFailed {
		return fmt.Errorf("transfer state %s cannot be resumed", t.State)
	}
	if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StatePending; t.Error = ""; return nil }); err != nil {
		return err
	}
	m.start(id)
	return nil
}

// Cancel stops a transfer and removes its local staging artifacts.
func (m *Manager) Cancel(id string) error {
	t, ok := m.store.GetTransfer(id)
	if !ok {
		return os.ErrNotExist
	}
	if t.State == StateSuccess || t.State == StateCancelled {
		return fmt.Errorf("transfer is already %s", t.State)
	}
	m.cancel(id)
	if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StateCancelled; return nil }); err != nil {
		return err
	}
	if t.LocalPath != "" {
		_ = os.Remove(t.LocalPath)
	}
	m.cleanupImportFiles(id)
	if t.Kind == KindDriveUpload && t.UploadID != "" {
		if upload, exists := m.store.GetUpload(t.UploadID); exists {
			_ = os.Remove(upload.Path)
		}
		_ = m.store.DeleteUpload(t.UploadID)
	}
	return nil
}

func (m *Manager) start(id string) {
	m.mu.Lock()
	if _, exists := m.cancels[id]; exists {
		m.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	m.cancels[id] = cancel
	m.mu.Unlock()
	go func() {
		defer func() {
			m.mu.Lock()
			delete(m.cancels, id)
			m.mu.Unlock()
		}()
		t, ok := m.store.GetTransfer(id)
		if !ok {
			return
		}
		var err error
		switch t.Kind {
		case KindDriveUpload:
			err = m.runDriveUpload(ctx, id)
		case KindPythonImport:
			err = m.runPythonImport(ctx, id)
		default:
			err = fmt.Errorf("unknown transfer kind %q", t.Kind)
		}
		if err != nil && ctx.Err() == nil {
			_, _ = m.store.UpdateTransfer(id, func(t *store.Transfer) error {
				t.State = StateFailed
				t.Error = err.Error()
				return nil
			})
		}
	}()
}

func (m *Manager) cancel(id string) {
	m.mu.Lock()
	if cancel, ok := m.cancels[id]; ok {
		cancel()
	}
	m.mu.Unlock()
}

func (m *Manager) runDriveUpload(ctx context.Context, id string) error {
	if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StateTransferring; return nil }); err != nil {
		return err
	}
	t, _ := m.store.GetTransfer(id)
	file, err := os.Open(t.LocalPath)
	if err != nil {
		return fmt.Errorf("open upload staging file: %w", err)
	}
	defer file.Close()
	if t.TotalBytes == 0 {
		return errors.New("不能上传空文件")
	}
	stat, err := file.Stat()
	if err != nil {
		return err
	}
	if stat.Size() != t.TotalBytes {
		return fmt.Errorf("upload staging file size mismatch: expected=%d actual=%d", t.TotalBytes, stat.Size())
	}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		t, _ = m.store.GetTransfer(id)
		if t.SessionURL == "" {
			// A Drive resumable session is opaque and may expire. Resetting both
			// URL and offset ensures a replacement session never skips bytes.
			if t.Transferred != 0 {
				if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.Transferred = 0; return nil }); err != nil {
					return err
				}
				t.Transferred = 0
			}
			session, sessionErr := m.drive.StartUploadSession(ctx, t.FileName, t.MIME, t.TotalBytes)
			if sessionErr != nil {
				return sessionErr
			}
			if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.SessionURL = session.URL; return nil }); err != nil {
				return err
			}
			t.SessionURL = session.URL
		}
		result, chunkErr := m.drive.UploadChunk(ctx, t.SessionURL, file, t.Transferred, t.TotalBytes, m.cfg.ChunkSizeBytes)
		if chunkErr != nil {
			if strings.Contains(chunkErr.Error(), "session expired") {
				_, _ = m.store.UpdateTransfer(id, func(t *store.Transfer) error {
					t.SessionURL = ""
					t.Transferred = 0
					t.State = StateRetrying
					return nil
				})
				continue
			}
			return chunkErr
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		_, err = m.store.UpdateTransfer(id, func(t *store.Transfer) error {
			t.Transferred = result.NextOffset
			if result.File != nil {
				t.FileID = result.File.ID
			}
			return nil
		})
		if err != nil {
			return err
		}
		if result.Completed {
			if err := ctx.Err(); err != nil {
				return err
			}
			_, err = m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StateSuccess; return nil })
			_ = os.Remove(t.LocalPath)
			if t.UploadID != "" {
				_ = m.store.DeleteUpload(t.UploadID)
			}
			return err
		}
	}
}

func (m *Manager) runPythonImport(ctx context.Context, id string) error {
	if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StateTransferring; return nil }); err != nil {
		return err
	}
	t, _ := m.store.GetTransfer(id)
	metadata, err := m.drive.Metadata(ctx, t.FileID)
	if err != nil {
		return err
	}
	if metadata.MimeType == "application/vnd.google-apps.folder" {
		return errors.New("不能把 Drive 文件夹导入 Python 流水线")
	}
	if metadata.Size <= 0 {
		return errors.New("Drive 文件为空或缺少可下载的 size 元数据")
	}
	name := filepath.Base(metadata.Name)
	if name == "." || name == string(filepath.Separator) || name == "" {
		name = "drive-import.bin"
	}
	partPath := filepath.Join(m.cfg.StagingDir(), id+"-"+name+".part")
	readyPath := strings.TrimSuffix(partPath, ".part")
	if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error {
		t.FileName = name
		t.MIME = metadata.MimeType
		t.LocalPath = readyPath
		t.TotalBytes = metadata.Size
		t.ExpectedMD5 = metadata.Md5Checksum
		t.ETag = metadata.Etag
		return nil
	}); err != nil {
		return err
	}
	if err := os.MkdirAll(m.cfg.StagingDir(), 0o700); err != nil {
		return err
	}
	readyStat, readyErr := os.Stat(readyPath)
	if readyErr == nil && readyStat.Size() == metadata.Size {
		_, _ = m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.Transferred = metadata.Size; return nil })
	} else {
		if readyErr == nil {
			_ = os.Remove(readyPath)
		}
		if err := m.downloadToFile(ctx, id, t.FileID, partPath, readyPath, metadata.Size); err != nil {
			return err
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if metadata.Md5Checksum != "" {
		_, _ = m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StateVerifying; return nil })
		sum, err := md5File(readyPath)
		if err != nil {
			return err
		}
		if sum != metadata.Md5Checksum {
			return fmt.Errorf("Drive 文件 MD5 校验失败：expected=%s actual=%s", metadata.Md5Checksum, sum)
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := m.postPythonUpload(ctx, id, readyPath, name); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	_, err = m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StateSuccess; return nil })
	_ = os.Remove(readyPath)
	return err
}

func (m *Manager) downloadToFile(ctx context.Context, transferID, fileID, partPath, readyPath string, total int64) error {
	file, err := os.OpenFile(partPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	closed := false
	defer func() {
		if !closed {
			_ = file.Close()
		}
	}()
	finish := func() error {
		if err := file.Sync(); err != nil {
			return err
		}
		if err := file.Close(); err != nil {
			return err
		}
		closed = true
		return os.Rename(partPath, readyPath)
	}
	if stat, statErr := file.Stat(); statErr == nil && stat.Size() > total {
		if err := file.Truncate(0); err != nil {
			return err
		}
	}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		stat, err := file.Stat()
		if err != nil {
			return err
		}
		offset := stat.Size()
		if offset >= total {
			if offset > total {
				if err := file.Truncate(0); err != nil {
					return err
				}
				_, _ = m.store.UpdateTransfer(transferID, func(t *store.Transfer) error { t.Transferred = 0; return nil })
				continue
			}
			return finish()
		}
		_, response, err := m.drive.DownloadRange(ctx, fileID, fmt.Sprintf("bytes=%d-", offset))
		if err != nil {
			if err := retrySleep(ctx); err != nil {
				return err
			}
			continue
		}
		if response.StatusCode != http.StatusOK && response.StatusCode != http.StatusPartialContent {
			_ = response.Body.Close()
			return fmt.Errorf("Drive range download returned %s", response.Status)
		}
		if offset > 0 && response.StatusCode == http.StatusOK {
			// A server that ignores Range would otherwise append the full file to
			// the partial file. Restart from zero so the local artifact remains
			// verifiable instead of silently corrupting it.
			_ = response.Body.Close()
			if err := file.Truncate(0); err != nil {
				return err
			}
			_, _ = m.store.UpdateTransfer(transferID, func(t *store.Transfer) error { t.Transferred = 0; return nil })
			continue
		}
		if _, err := file.Seek(offset, io.SeekStart); err != nil {
			_ = response.Body.Close()
			return err
		}
		buf := make([]byte, 1024*1024)
		for {
			n, readErr := response.Body.Read(buf)
			if n > 0 {
				if offset+int64(n) > total {
					_ = response.Body.Close()
					return fmt.Errorf("Drive response exceeds expected size %d", total)
				}
				written, writeErr := file.Write(buf[:n])
				if writeErr != nil || written != n {
					_ = response.Body.Close()
					if writeErr != nil {
						return writeErr
					}
					return io.ErrShortWrite
				}
				newOffset := offset + int64(n)
				_, _ = m.store.UpdateTransfer(transferID, func(t *store.Transfer) error { t.Transferred = newOffset; return nil })
				offset = newOffset
			}
			if readErr != nil {
				_ = response.Body.Close()
				if errors.Is(readErr, io.EOF) {
					break
				}
				if err := retrySleep(ctx); err != nil {
					return err
				}
				break
			}
		}
		if offset >= total {
			return finish()
		}
	}
}

func (m *Manager) cleanupImportFiles(transferID string) {
	paths, err := filepath.Glob(filepath.Join(m.cfg.StagingDir(), transferID+"-*"))
	if err != nil {
		return
	}
	for _, path := range paths {
		_ = os.Remove(path)
	}
}

func (m *Manager) postPythonUpload(ctx context.Context, transferID, path, name string) error {
	if m.cfg.PythonBaseURL == "" {
		return errors.New("python_base_url 未配置")
	}
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	reader, writer := io.Pipe()
	form := multipart.NewWriter(writer)
	go func() {
		defer writer.Close()
		defer form.Close()
		_ = form.WriteField("sourceLang", "auto")
		_ = form.WriteField("targetLang", "zh-CN")
		_ = form.WriteField("mode", "mono")
		_ = form.WriteField("burn", "hard")
		_ = form.WriteField("model", "small")
		_ = form.WriteField("engine", "deepseek")
		_ = form.WriteField("needSubtitle", "true")
		part, createErr := form.CreateFormFile("file", name)
		if createErr != nil {
			_ = writer.CloseWithError(createErr)
			return
		}
		if _, copyErr := io.Copy(part, file); copyErr != nil {
			_ = writer.CloseWithError(copyErr)
		}
	}()
	endpoint := strings.TrimRight(m.cfg.PythonBaseURL, "/") + "/api/tasks/upload"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", form.FormDataContentType())
	if m.cfg.PythonAPIToken != "" {
		req.Header.Set("X-API-Token", m.cfg.PythonAPIToken)
	}
	client := &http.Client{Timeout: 0}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("调用 Python upload API: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Python upload API 返回 %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	var result struct {
		ID string `json:"id"`
	}
	if json.Unmarshal(body, &result) == nil && result.ID != "" {
		_, _ = m.store.UpdateTransfer(transferID, func(t *store.Transfer) error { t.PythonTaskID = result.ID; return nil })
	}
	return nil
}

func md5File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := md5.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func retrySleep(ctx context.Context) error {
	t := time.NewTimer(2 * time.Second)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
