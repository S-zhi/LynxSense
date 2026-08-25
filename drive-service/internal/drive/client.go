// Package drive 是面向 sidecar 的轻量 Google Drive v3 客户端，覆盖文件列表、
// 软删除、断点上传和范围下载四项基础操作。
package drive

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	"github.com/s-zhi/subtitles-ai-drive/internal/oauth"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
)

const (
	apiBase    = "https://www.googleapis.com/drive/v3"
	uploadBase = "https://www.googleapis.com/upload/drive/v3"
	folderMIME = "application/vnd.google-apps.folder"
)

// Capabilities 描述 HTTP API 所需的 Drive 文件权限。
type Capabilities struct {
	CanDownload bool `json:"canDownload"`
}

// File 是 sidecar 对外暴露的 Drive 文件元数据子集。
type File struct {
	ID             string       `json:"id"`
	Name           string       `json:"name"`
	MimeType       string       `json:"mimeType"`
	Size           int64        `json:"size,string"`
	ModifiedTime   string       `json:"modifiedTime"`
	Md5Checksum    string       `json:"md5Checksum"`
	WebContentLink string       `json:"webContentLink"`
	Capabilities   Capabilities `json:"capabilities"`
	Parents        []string     `json:"parents"`
	Trashed        bool         `json:"trashed"`
	Etag           string       `json:"etag"`
}

// FileList 是 Drive 文件接口的分页响应。
type FileList struct {
	NextPageToken string `json:"nextPageToken"`
	Files         []File `json:"files"`
}

// Client 持有已认证的 Drive 通道和缓存的应用目录 ID，可由所有 HTTP handler
// 共享同一个实例。
type Client struct {
	cfg   *config.Config
	oauth *oauth.Manager
	store *store.Store
}

// NewClient 创建进程级 Drive 客户端。认证采用延迟解析，因此启动服务时不要求
// 立即完成浏览器登录。
func NewClient(cfg *config.Config, auth *oauth.Manager, state *store.Store) *Client {
	return &Client{cfg: cfg, oauth: auth, store: state}
}

// ensureFolder 获取或创建 sidecar 专用的 Drive 文件夹，并将其 ID 缓存到状态仓库。
func (c *Client) ensureFolder(ctx context.Context) (string, error) {
	if c.cfg.DriveFolderID != "" {
		return c.cfg.DriveFolderID, nil
	}
	// 将目录 ID 缓存到持久化状态中，避免每次重启都查询 Drive，也避免重复创建目录。
	if id := c.store.DriveFolderID(); id != "" {
		return id, nil
	}
	q := fmt.Sprintf("name = 'Subtitles AI' and mimeType = '%s' and trashed = false", folderMIME)
	values := url.Values{
		"q": {q}, "spaces": {"drive"}, "pageSize": {"10"},
		"fields": {"files(id,name,mimeType,parents)"},
	}
	var result FileList
	if err := c.doJSON(ctx, http.MethodGet, apiBase+"/files?"+values.Encode(), nil, nil, &result); err != nil {
		return "", fmt.Errorf("find Drive folder: %w", err)
	}
	if len(result.Files) > 0 {
		if err := c.store.SetDriveFolderID(result.Files[0].ID); err != nil {
			return "", err
		}
		return result.Files[0].ID, nil
	}
	body, _ := json.Marshal(map[string]any{"name": "Subtitles AI", "mimeType": folderMIME})
	var folder File
	if err := c.doJSON(ctx, http.MethodPost, apiBase+"/files?supportsAllDrives=true&fields=id,name,mimeType,parents", bytes.NewReader(body), map[string]string{"Content-Type": "application/json"}, &folder); err != nil {
		return "", fmt.Errorf("create Drive folder: %w", err)
	}
	if folder.ID == "" {
		return "", errors.New("Drive folder response has no id")
	}
	if err := c.store.SetDriveFolderID(folder.ID); err != nil {
		return "", err
	}
	return folder.ID, nil
}

// List 返回此 sidecar 应用目录下的文件。
func (c *Client) List(ctx context.Context, pageToken string, pageSize int64) (*FileList, error) {
	folderID, err := c.ensureFolder(ctx)
	if err != nil {
		return nil, err
	}
	if pageSize <= 0 || pageSize > 1000 {
		pageSize = 100
	}
	q := fmt.Sprintf("'%s' in parents and trashed = false", escapeQueryLiteral(folderID))
	values := url.Values{
		"q": {q}, "spaces": {"drive"}, "pageSize": {strconv.FormatInt(pageSize, 10)},
		"orderBy": {"modifiedTime desc"}, "supportsAllDrives": {"true"},
		"includeItemsFromAllDrives": {"true"},
		"fields":                    {"nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum,webContentLink,capabilities,parents,trashed)"},
	}
	if pageToken != "" {
		values.Set("pageToken", pageToken)
	}
	var result FileList
	if err := c.doJSON(ctx, http.MethodGet, apiBase+"/files?"+values.Encode(), nil, nil, &result); err != nil {
		return nil, err
	}
	return &result, nil
}

// Metadata 获取单个 Drive 文件的元数据。
func (c *Client) Metadata(ctx context.Context, fileID string) (*File, error) {
	values := url.Values{
		"fields":            {"id,name,mimeType,size,modifiedTime,md5Checksum,capabilities,trashed,parents"},
		"supportsAllDrives": {"true"},
	}
	var result File
	if err := c.doJSON(ctx, http.MethodGet, apiBase+"/files/"+url.PathEscape(fileID)+"?"+values.Encode(), nil, nil, &result); err != nil {
		return nil, err
	}
	return &result, nil
}

// Trash 将文件移入 Drive 回收站，而不是执行不可逆删除。
func (c *Client) Trash(ctx context.Context, fileID string) error {
	body, _ := json.Marshal(map[string]bool{"trashed": true})
	return c.doJSON(ctx, http.MethodPatch, apiBase+"/files/"+url.PathEscape(fileID)+"?supportsAllDrives=true&fields=id,trashed", bytes.NewReader(body), map[string]string{"Content-Type": "application/json"}, &File{})
}

// DownloadRange 返回已认证的媒体响应。响应体由调用方负责关闭。
func (c *Client) DownloadRange(ctx context.Context, fileID, rangeHeader string) (*File, *http.Response, error) {
	metadata, err := c.Metadata(ctx, fileID)
	if err != nil {
		return nil, nil, err
	}
	client, err := c.oauth.HTTPClient(ctx)
	if err != nil {
		return nil, nil, err
	}
	endpoint := apiBase + "/files/" + url.PathEscape(fileID) + "?alt=media&supportsAllDrives=true"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, nil, err
	}
	if rangeHeader != "" {
		req.Header.Set("Range", rangeHeader)
	}
	// Range 偏移量基于原始媒体的字节位置。禁用透明 gzip，避免响应编码改变
	// 断点下载的字节计数。
	req.Header.Set("Accept-Encoding", "identity")
	resp, err := client.Do(req)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 16*1024))
		return nil, nil, fmt.Errorf("Drive download failed: %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	return metadata, resp, nil
}

// UploadSession 标识一个 Drive 断点上传会话。
type UploadSession struct{ URL string }

// StartUploadSession 创建断点上传会话并返回不透明的会话 URL。调用方可以安全地
// 重试单个分片，而无需重复发送已经确认的字节。
func (c *Client) StartUploadSession(ctx context.Context, name, mime string, size int64) (UploadSession, error) {
	if size <= 0 {
		return UploadSession{}, errors.New("Drive upload size must be positive")
	}
	if mime == "" {
		mime = "application/octet-stream"
	}
	folderID, err := c.ensureFolder(ctx)
	if err != nil {
		return UploadSession{}, err
	}
	client, err := c.oauth.HTTPClient(ctx)
	if err != nil {
		return UploadSession{}, err
	}
	metadata := map[string]any{
		"name": filepath.Base(name), "mimeType": mime, "parents": []string{folderID},
		"appProperties": map[string]string{"subtitles_ai": "true"},
	}
	body, err := json.Marshal(metadata)
	if err != nil {
		return UploadSession{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, uploadBase+"/files?uploadType=resumable&supportsAllDrives=true", bytes.NewReader(body))
	if err != nil {
		return UploadSession{}, err
	}
	req.Header.Set("Content-Type", "application/json; charset=UTF-8")
	req.Header.Set("X-Upload-Content-Type", mime)
	req.Header.Set("X-Upload-Content-Length", strconv.FormatInt(size, 10))
	resp, err := client.Do(req)
	if err != nil {
		return UploadSession{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		responseBody, _ := io.ReadAll(io.LimitReader(resp.Body, 16*1024))
		return UploadSession{}, fmt.Errorf("start Drive upload session: %s: %s", resp.Status, strings.TrimSpace(string(responseBody)))
	}
	location := resp.Header.Get("Location")
	if location == "" {
		return UploadSession{}, errors.New("Drive upload session response has no Location header")
	}
	return UploadSession{URL: location}, nil
}

// ChunkResult 表示 Drive 已确认的断点上传进度。
type ChunkResult struct {
	NextOffset int64
	Completed  bool
	File       *File
}

// UploadChunk 向 Drive 发送一个分片。308 响应会推进调用方偏移量；404 表示不透明
// 会话已过期，需要创建新会话。
func (c *Client) UploadChunk(ctx context.Context, sessionURL string, file *os.File, offset, total, chunkSize int64) (ChunkResult, error) {
	if offset >= total {
		return ChunkResult{NextOffset: total, Completed: true}, nil
	}
	remaining := total - offset
	length := chunkSize
	if length <= 0 || length > remaining {
		length = remaining
	}
	for attempt := 0; attempt < 6; attempt++ {
		if _, err := file.Seek(offset, io.SeekStart); err != nil {
			return ChunkResult{}, err
		}
		reader := io.NewSectionReader(file, offset, length)
		client, err := c.oauth.HTTPClient(ctx)
		if err != nil {
			return ChunkResult{}, err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPut, sessionURL, reader)
		if err != nil {
			return ChunkResult{}, err
		}
		req.ContentLength = length
		req.Header.Set("Content-Length", strconv.FormatInt(length, 10))
		req.Header.Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", offset, offset+length-1, total))
		resp, err := client.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return ChunkResult{}, ctx.Err()
			}
			if err := backoff(ctx, attempt); err != nil {
				return ChunkResult{}, err
			}
			continue
		}
		responseBody, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
		status := resp.StatusCode
		rangeHeader := resp.Header.Get("Range")
		_ = resp.Body.Close()
		if status == http.StatusOK || status == http.StatusCreated {
			var uploaded File
			if len(responseBody) > 0 {
				_ = json.Unmarshal(responseBody, &uploaded)
			}
			return ChunkResult{NextOffset: total, Completed: true, File: &uploaded}, nil
		}
		if status == http.StatusPermanentRedirect {
			next := parseDriveRange(rangeHeader)
			if next == 0 {
				next = offset + length
			}
			if next <= offset || next > total {
				return ChunkResult{}, fmt.Errorf("Drive upload returned invalid next offset %d", next)
			}
			return ChunkResult{NextOffset: next}, nil
		}
		if status == http.StatusNotFound {
			return ChunkResult{}, errors.New("Drive upload session expired")
		}
		if status == http.StatusTooManyRequests || status >= 500 || status == http.StatusForbidden {
			if err := backoff(ctx, attempt); err != nil {
				return ChunkResult{}, err
			}
			continue
		}
		return ChunkResult{}, fmt.Errorf("Drive upload chunk failed: %s: %s", http.StatusText(status), strings.TrimSpace(string(responseBody)))
	}
	return ChunkResult{}, errors.New("Drive upload chunk retry limit exceeded")
}

// doJSON 执行已认证的 JSON Drive 请求，统一处理响应读取、状态码和反序列化。
func (c *Client) doJSON(ctx context.Context, method, endpoint string, body io.Reader, headers map[string]string, output any) error {
	client, err := c.oauth.HTTPClient(ctx)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, body)
	if err != nil {
		return err
	}
	for key, value := range headers {
		req.Header.Set(key, value)
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	responseBody, _ := io.ReadAll(io.LimitReader(resp.Body, 128*1024))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Drive API %s: %s", resp.Status, strings.TrimSpace(string(responseBody)))
	}
	if output != nil && len(responseBody) > 0 {
		if err := json.Unmarshal(responseBody, output); err != nil {
			return err
		}
	}
	return nil
}

// parseDriveRange 将 Drive 返回的 Range 末尾位置转换为下一个字节偏移量。
func parseDriveRange(value string) int64 {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0
	}
	parts := strings.Split(value, "-")
	if len(parts) != 2 {
		return 0
	}
	last, err := strconv.ParseInt(strings.TrimSpace(parts[1]), 10, 64)
	if err != nil || last < 0 {
		return 0
	}
	return last + 1
}

// backoff 按指数退避等待下一次 Drive 请求，并在上下文取消时立即返回。
func backoff(ctx context.Context, attempt int) error {
	d := time.Duration(1<<min(attempt, 5)) * time.Second
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// min 返回两个整数中的较小值，用于限制重试退避的最大指数。
func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// escapeQueryLiteral 转义 Drive 查询字符串中的单引号，避免破坏查询表达式。
func escapeQueryLiteral(value string) string { return strings.ReplaceAll(value, "'", "\\'") }
