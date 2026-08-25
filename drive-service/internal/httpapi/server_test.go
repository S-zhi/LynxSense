package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	driveclient "github.com/s-zhi/subtitles-ai-drive/internal/drive"
	"github.com/s-zhi/subtitles-ai-drive/internal/oauth"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
	"github.com/s-zhi/subtitles-ai-drive/internal/transfer"
)

// TestUploadEndpointSupportsOffsetChecks 验证分片上传的偏移校验、查询和删除流程。
func TestUploadEndpointSupportsOffsetChecks(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	cfg.PythonBaseURL = ""
	if err := cfg.EnsureDataDir(); err != nil {
		t.Fatal(err)
	}
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	auth := oauth.NewManager(&cfg)
	drive := driveclient.NewClient(&cfg, auth, state)
	transfers := transfer.NewManager(&cfg, state, drive)
	server := New(&cfg, auth, drive, transfers, state)

	createReq := httptest.NewRequest(http.MethodPost, "/api/drive/uploads", nil)
	createReq.Header.Set("X-Upload-Length", "10")
	createReq.Header.Set("X-File-Name", "video.mp4")
	createReq.Header.Set("X-File-Mime", "video/mp4")
	createResp := httptest.NewRecorder()
	server.ServeHTTP(createResp, createReq)
	if createResp.Code != http.StatusCreated {
		t.Fatalf("create status = %d body=%s", createResp.Code, createResp.Body.String())
	}
	var created struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(createResp.Body.Bytes(), &created); err != nil {
		t.Fatal(err)
	}
	if created.ID == "" {
		t.Fatal("create response has no upload id")
	}

	patchReq := httptest.NewRequest(http.MethodPatch, "/api/drive/uploads/"+created.ID, bytes.NewBufferString("abc"))
	patchReq.Header.Set("X-Upload-Offset", "0")
	patchResp := httptest.NewRecorder()
	server.ServeHTTP(patchResp, patchReq)
	if patchResp.Code != http.StatusNoContent || patchResp.Header().Get("Upload-Offset") != "3" {
		t.Fatalf("patch status=%d offset=%q body=%s", patchResp.Code, patchResp.Header().Get("Upload-Offset"), patchResp.Body.String())
	}

	headReq := httptest.NewRequest(http.MethodHead, "/api/drive/uploads/"+created.ID, nil)
	headResp := httptest.NewRecorder()
	server.ServeHTTP(headResp, headReq)
	if headResp.Code != http.StatusNoContent || headResp.Header().Get("Upload-Offset") != "3" {
		t.Fatalf("head status=%d offset=%q", headResp.Code, headResp.Header().Get("Upload-Offset"))
	}

	mismatchReq := httptest.NewRequest(http.MethodPatch, "/api/drive/uploads/"+created.ID, bytes.NewBufferString("x"))
	mismatchReq.Header.Set("X-Upload-Offset", "0")
	mismatchResp := httptest.NewRecorder()
	server.ServeHTTP(mismatchResp, mismatchReq)
	if mismatchResp.Code != http.StatusConflict || mismatchResp.Header().Get("Upload-Offset") != "3" {
		t.Fatalf("mismatch status=%d offset=%q body=%s", mismatchResp.Code, mismatchResp.Header().Get("Upload-Offset"), mismatchResp.Body.String())
	}

	deleteReq := httptest.NewRequest(http.MethodDelete, "/api/drive/uploads/"+created.ID, nil)
	deleteResp := httptest.NewRecorder()
	server.ServeHTTP(deleteResp, deleteReq)
	if deleteResp.Code != http.StatusNoContent {
		t.Fatalf("delete status=%d body=%s", deleteResp.Code, deleteResp.Body.String())
	}
	missingHead := httptest.NewRecorder()
	server.ServeHTTP(missingHead, headReq)
	if missingHead.Code != http.StatusNotFound {
		t.Fatalf("deleted upload HEAD status=%d", missingHead.Code)
	}
}

// TestLocalhostCORSOriginIsReflected 验证允许的本地前端来源会收到 CORS 响应头。
func TestLocalhostCORSOriginIsReflected(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	auth := oauth.NewManager(&cfg)
	drive := driveclient.NewClient(&cfg, auth, state)
	transfers := transfer.NewManager(&cfg, state, drive)
	server := New(&cfg, auth, drive, transfers, state)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	req.Header.Set("Origin", "http://localhost:8000")
	resp := httptest.NewRecorder()
	server.ServeHTTP(resp, req)
	if got := resp.Header().Get("Access-Control-Allow-Origin"); got != "http://localhost:8000" {
		t.Fatalf("allow origin = %q", got)
	}
}

func TestFolderUploadManifestIsSafeAndIdempotent(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	auth := oauth.NewManager(&cfg)
	drive := driveclient.NewClient(&cfg, auth, state)
	transfers := transfer.NewManager(&cfg, state, drive)
	server := New(&cfg, auth, drive, transfers, state)

	unsafeReq := httptest.NewRequest(http.MethodPost, "/api/drive/folder-uploads", bytes.NewBufferString(`{"entries":[{"relativePath":"../escape.txt","size":1}]}`))
	unsafeReq.Header.Set("Content-Type", "application/json")
	unsafeResp := httptest.NewRecorder()
	server.ServeHTTP(unsafeResp, unsafeReq)
	if unsafeResp.Code != http.StatusBadRequest {
		t.Fatalf("unsafe manifest status = %d body=%s", unsafeResp.Code, unsafeResp.Body.String())
	}

	body := `{"entries":[{"relativePath":"video.mp4","size":1,"mime":"video/mp4"}]}`
	createReq := httptest.NewRequest(http.MethodPost, "/api/drive/folder-uploads", bytes.NewBufferString(body))
	createReq.Header.Set("Content-Type", "application/json")
	createReq.Header.Set("Idempotency-Key", "folder-request-1")
	createResp := httptest.NewRecorder()
	server.ServeHTTP(createResp, createReq)
	if createResp.Code != http.StatusCreated {
		t.Fatalf("create folder upload status = %d body=%s", createResp.Code, createResp.Body.String())
	}
	var first folderUploadResponse
	if err := json.Unmarshal(createResp.Body.Bytes(), &first); err != nil {
		t.Fatal(err)
	}
	if first.Batch.ID == "" || len(first.Entries) != 1 || first.Entries[0].RelativePath != "video.mp4" {
		t.Fatalf("folder response = %#v", first)
	}

	retryReq := httptest.NewRequest(http.MethodPost, "/api/drive/folder-uploads", bytes.NewBufferString(body))
	retryReq.Header.Set("Content-Type", "application/json")
	retryReq.Header.Set("Idempotency-Key", "folder-request-1")
	retryResp := httptest.NewRecorder()
	server.ServeHTTP(retryResp, retryReq)
	if retryResp.Code != http.StatusOK {
		t.Fatalf("idempotent retry status = %d body=%s", retryResp.Code, retryResp.Body.String())
	}
	var second folderUploadResponse
	if err := json.Unmarshal(retryResp.Body.Bytes(), &second); err != nil {
		t.Fatal(err)
	}
	if second.Batch.ID != first.Batch.ID || len(second.Entries) != 1 || second.Entries[0].ID != first.Entries[0].ID {
		t.Fatalf("idempotent retry = %#v, first=%#v", second, first)
	}
}
