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
	apiBase         = "https://www.googleapis.com/drive/v3"
	uploadBase      = "https://www.googleapis.com/upload/drive/v3"
	folderMIME      = "application/vnd.google-apps.folder"
	rootFolderName  = "Subtitles AI"
	appPropertyKey  = "subtitles_ai"
	rootPropertyKey = "subtitles_ai_root"
)

// Capabilities 描述 HTTP API 所需的 Drive 文件权限。
type Capabilities struct {
	CanDownload bool `json:"canDownload"`
}

// File 是 sidecar 对外暴露的 Drive 文件元数据子集。
type File struct {
	ID             string            `json:"id"`
	Name           string            `json:"name"`
	MimeType       string            `json:"mimeType"`
	Size           int64             `json:"size,string"`
	ModifiedTime   string            `json:"modifiedTime"`
	Md5Checksum    string            `json:"md5Checksum"`
	WebContentLink string            `json:"webContentLink"`
	Capabilities   Capabilities      `json:"capabilities"`
	Parents        []string          `json:"parents"`
	Trashed        bool              `json:"trashed"`
	Etag           string            `json:"etag"`
	AppProperties  map[string]string `json:"appProperties"`
}

// FileList 是 Drive 文件接口的分页响应。
type FileList struct {
	NextPageToken string `json:"nextPageToken"`
	Files         []File `json:"files"`
	RootFolderID  string `json:"rootFolderId,omitempty"`
	CurrentFolder *File  `json:"currentFolder,omitempty"`
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
	if c == nil {
		return "", errors.New("Drive client is nil")
	}
	if c.cfg != nil && c.cfg.DriveFolderID != "" {
		return c.cfg.DriveFolderID, nil
	}
	// 将目录 ID 缓存到持久化状态中，避免每次重启都查询 Drive，也避免重复创建目录。
	if c.store != nil {
		if id := c.store.DriveFolderID(); id != "" {
			return id, nil
		}
	}
	// Prefer the explicit marker.  The name-only query below is retained for
	// installations created by older versions of the sidecar.
	q := fmt.Sprintf("appProperties has { key = '%s' and value = 'true' } and mimeType = '%s' and trashed = false", rootPropertyKey, folderMIME)
	values := url.Values{
		"q":                         {q},
		"spaces":                    {"drive"},
		"pageSize":                  {"10"},
		"supportsAllDrives":         {"true"},
		"includeItemsFromAllDrives": {"true"},
		"fields":                    {"files(id,name,mimeType,parents,trashed,appProperties)"},
	}
	var result FileList
	if err := c.doJSON(ctx, http.MethodGet, apiBase+"/files?"+values.Encode(), nil, nil, &result); err != nil {
		return "", fmt.Errorf("find Drive folder: %w", err)
	}
	if rootID := firstUsableFolderID(result.Files); rootID != "" {
		if err := c.cacheRootID(rootID); err != nil {
			return "", err
		}
		return rootID, nil
	}
	q = fmt.Sprintf("name = '%s' and mimeType = '%s' and trashed = false", rootFolderName, folderMIME)
	values.Set("q", q)
	result = FileList{}
	if err := c.doJSON(ctx, http.MethodGet, apiBase+"/files?"+values.Encode(), nil, nil, &result); err != nil {
		return "", fmt.Errorf("find Drive folder: %w", err)
	}
	if rootID := firstUsableFolderID(result.Files); rootID != "" {
		if err := c.cacheRootID(rootID); err != nil {
			return "", err
		}
		return rootID, nil
	}
	folder, err := c.createFolder(ctx, rootFolderName, "", map[string]string{rootPropertyKey: "true"})
	if err != nil {
		return "", fmt.Errorf("create Drive folder: %w", err)
	}
	if folder.ID == "" {
		return "", errors.New("Drive folder response has no id")
	}
	if c.store != nil {
		if err := c.store.SetDriveFolderID(folder.ID); err != nil {
			return "", err
		}
	}
	return folder.ID, nil
}

func firstUsableFolderID(files []File) string {
	for _, file := range files {
		// The Drive query constrains mimeType and trashed. Keep the ID-only
		// acceptance for older responses that omitted optional projections.
		if file.ID != "" && !file.Trashed {
			return file.ID
		}
	}
	return ""
}

func (c *Client) cacheRootID(id string) error {
	if c.store == nil {
		return nil
	}
	return c.store.SetDriveFolderID(id)
}

// IsFolder reports whether metadata describes a Drive folder.
func IsFolder(file *File) bool { return file != nil && file.MimeType == folderMIME }

// CreateFolder creates a folder below parentID. An empty parentID means the
// sidecar root; callers can therefore create nested folders without knowing
// the configured root ID.
func (c *Client) CreateFolder(ctx context.Context, name, parentID string, appProperties map[string]string) (*File, error) {
	folderName, err := cleanDriveName(name)
	if err != nil {
		return nil, err
	}
	if parentID == "" {
		parentID, err = c.ensureFolder(ctx)
		if err != nil {
			return nil, err
		}
	}
	return c.createFolder(ctx, folderName, parentID, appProperties)
}

func (c *Client) createFolder(ctx context.Context, name, parentID string, appProperties map[string]string) (*File, error) {
	props := make(map[string]string, len(appProperties)+1)
	for k, v := range appProperties {
		props[k] = v
	}
	// Every folder/file created by this client remains identifiable as an
	// application-owned object.  Callers may add their own properties, but may
	// not turn off the ownership marker.
	props[appPropertyKey] = "true"
	metadata := map[string]any{"name": name, "mimeType": folderMIME, "appProperties": props}
	if parentID != "" {
		metadata["parents"] = []string{parentID}
	}
	body, err := json.Marshal(metadata)
	if err != nil {
		return nil, err
	}
	var folder File
	endpoint := apiBase + "/files?supportsAllDrives=true&fields=id,name,mimeType,parents,trashed,appProperties"
	if err := c.doJSON(ctx, http.MethodPost, endpoint, bytes.NewReader(body), map[string]string{"Content-Type": "application/json"}, &folder); err != nil {
		return nil, err
	}
	if folder.ID == "" || !IsFolder(&folder) {
		return nil, errors.New("Drive folder response is invalid")
	}
	return &folder, nil
}

// ValidateFolderUnderRoot resolves folderID and verifies it is a live folder
// strictly below the sidecar root. It rejects the root itself and unrelated
// Drive folders.
func (c *Client) ValidateFolderUnderRoot(ctx context.Context, folderID string) (*File, error) {
	folderID = strings.TrimSpace(folderID)
	if folderID == "" {
		return nil, errors.New("Drive folder ID is empty")
	}
	rootID, err := c.ensureFolder(ctx)
	if err != nil {
		return nil, err
	}
	if folderID == rootID {
		return nil, errors.New("Drive root folder cannot be used as a target")
	}
	seen := map[string]bool{folderID: true}
	queue := []string{folderID}
	var target *File
	for len(queue) > 0 {
		id := queue[0]
		queue = queue[1:]
		file, err := c.Metadata(ctx, id)
		if err != nil {
			return nil, err
		}
		if file == nil {
			return nil, errors.New("Drive target metadata is empty")
		}
		if file.Trashed {
			return nil, errors.New("Drive target folder is in the trash")
		}
		if !IsFolder(file) {
			if id == folderID {
				return nil, errors.New("Drive target is not a folder")
			}
			return nil, errors.New("Drive target has a non-folder parent")
		}
		if target == nil {
			target = file
		}
		for _, parent := range file.Parents {
			parent = strings.TrimSpace(parent)
			if parent == rootID {
				return target, nil
			}
			if parent != "" && !seen[parent] {
				seen[parent] = true
				queue = append(queue, parent)
			}
		}
	}
	return nil, errors.New("Drive target folder is outside the sidecar root")
}

// ResolveTargetFolder is an explicit alias for callers that need the
// validated metadata rather than only a boolean check.
func (c *Client) ResolveTargetFolder(ctx context.Context, folderID string) (*File, error) {
	return c.ValidateFolderUnderRoot(ctx, folderID)
}

// ResolveTargetFolderID validates a target and returns its stable Drive ID.
// It is useful to callers that do not need to expose the full metadata object.
func (c *Client) ResolveTargetFolderID(ctx context.Context, folderID string) (string, error) {
	folder, err := c.ValidateFolderUnderRoot(ctx, folderID)
	if err != nil {
		return "", err
	}
	return folder.ID, nil
}

// List 返回此 sidecar 应用目录下的文件。
func (c *Client) List(ctx context.Context, parentID, pageToken string, pageSize int64) (*FileList, error) {
	rootID, err := c.ensureFolder(ctx)
	if err != nil {
		return nil, err
	}
	parentID = strings.TrimSpace(parentID)
	var currentFolder *File
	if parentID == "" || parentID == rootID {
		parentID = rootID
		currentFolder = &File{ID: rootID, Name: rootFolderName, MimeType: folderMIME}
	} else {
		currentFolder, err = c.ValidateFolderUnderRoot(ctx, parentID)
		if err != nil {
			return nil, err
		}
	}
	if pageSize <= 0 || pageSize > 1000 {
		pageSize = 100
	}
	q := fmt.Sprintf("'%s' in parents and trashed = false", escapeQueryLiteral(parentID))
	values := url.Values{
		"q": {q}, "spaces": {"drive"}, "pageSize": {strconv.FormatInt(pageSize, 10)},
		"orderBy": {"modifiedTime desc"}, "supportsAllDrives": {"true"},
		"includeItemsFromAllDrives": {"true"},
		"fields":                    {"nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum,webContentLink,capabilities,parents,trashed,appProperties)"},
	}
	if pageToken != "" {
		values.Set("pageToken", pageToken)
	}
	var result FileList
	if err := c.doJSON(ctx, http.MethodGet, apiBase+"/files?"+values.Encode(), nil, nil, &result); err != nil {
		return nil, err
	}
	result.RootFolderID = rootID
	result.CurrentFolder = currentFolder
	return &result, nil
}

// Metadata 获取单个 Drive 文件的元数据。
func (c *Client) Metadata(ctx context.Context, fileID string) (*File, error) {
	values := url.Values{
		"fields":            {"id,name,mimeType,size,modifiedTime,md5Checksum,capabilities,trashed,parents,appProperties"},
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

// StartUploadSession creates a resumable upload in the application root. It
// preserves the original API for callers that do not need folder placement.
func (c *Client) StartUploadSession(ctx context.Context, name, mime string, size int64) (UploadSession, error) {
	return c.StartUploadSessionInFolder(ctx, name, mime, size, "", nil)
}

// StartUploadSessionInFolder 创建断点上传会话并返回不透明的会话 URL。调用方可以安全地
// 重试单个分片，而无需重复发送已经确认的字节。
func (c *Client) StartUploadSessionInFolder(ctx context.Context, name, mime string, size int64, parentID string, appProperties map[string]string) (UploadSession, error) {
	if size <= 0 {
		return UploadSession{}, errors.New("Drive upload size must be positive")
	}
	name, err := cleanDriveName(name)
	if err != nil {
		return UploadSession{}, err
	}
	if mime == "" {
		mime = "application/octet-stream"
	}
	if parentID == "" {
		parentID, err = c.ensureFolder(ctx)
		if err != nil {
			return UploadSession{}, err
		}
	}
	client, err := c.oauth.HTTPClient(ctx)
	if err != nil {
		return UploadSession{}, err
	}
	props := make(map[string]string, len(appProperties)+1)
	for k, v := range appProperties {
		props[k] = v
	}
	props[appPropertyKey] = "true"
	metadata := map[string]any{"name": name, "mimeType": mime, "parents": []string{parentID}, "appProperties": props}
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

// cleanDriveName normalizes names accepted by Drive file/folder creation and
// rejects values that would otherwise become the current directory marker.
func cleanDriveName(name string) (string, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		return "", errors.New("Drive file name is empty")
	}
	name = filepath.Base(name)
	if name == "" || name == "." || name == ".." || name == string(filepath.Separator) {
		return "", errors.New("Drive file name is invalid")
	}
	return name, nil
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
