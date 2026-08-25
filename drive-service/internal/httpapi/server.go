// Package httpapi exposes the local HTTP contract used by the web client and
// keeps transport concerns separate from OAuth, Drive, and transfer workers.
package httpapi

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	driveclient "github.com/s-zhi/subtitles-ai-drive/internal/drive"
	"github.com/s-zhi/subtitles-ai-drive/internal/oauth"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
	"github.com/s-zhi/subtitles-ai-drive/internal/transfer"
)

// Server routes OAuth, file, upload, and transfer requests to the shared
// process-wide service instances.
type Server struct {
	cfg       *config.Config
	auth      *oauth.Manager
	drive     *driveclient.Client
	transfers *transfer.Manager
	store     *store.Store
}

// New wires the HTTP facade to the single-user service dependencies.
func New(cfg *config.Config, auth *oauth.Manager, client *driveclient.Client, transfers *transfer.Manager, state *store.Store) *Server {
	return &Server{cfg: cfg, auth: auth, drive: client, transfers: transfers, store: state}
}

// ServeHTTP implements the complete local API. Long-running upload/download
// work is handed to transfer.Manager so request cancellation does not erase a
// durable checkpoint.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.setCommonHeaders(w, r)
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.URL.Path == "/healthz" {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
		return
	}
	if r.URL.Path == "/api/oauth/status" && r.Method == http.MethodGet {
		writeJSON(w, http.StatusOK, s.auth.Status())
		return
	}
	if r.URL.Path == "/api/oauth/google/start" && r.Method == http.MethodGet {
		url, err := s.auth.Start()
		if err != nil {
			writeError(w, http.StatusServiceUnavailable, err)
			return
		}
		http.Redirect(w, r, url, http.StatusFound)
		return
	}
	if r.URL.Path == "/api/oauth/google/callback" && r.Method == http.MethodGet {
		s.auth.Callback(w, r)
		return
	}
	if r.URL.Path == "/api/oauth/google/disconnect" && (r.Method == http.MethodPost || r.Method == http.MethodDelete) {
		if err := s.auth.Disconnect(r.Context()); err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"connected": false})
		return
	}

	if r.URL.Path == "/api/drive/files" && r.Method == http.MethodGet {
		s.handleListFiles(w, r)
		return
	}
	if r.URL.Path == "/api/drive/uploads" && r.Method == http.MethodPost {
		s.handleCreateUpload(w, r)
		return
	}
	if r.URL.Path == "/api/drive/transfers" && r.Method == http.MethodGet {
		writeJSON(w, http.StatusOK, map[string]any{"transfers": s.store.ListTransfers()})
		return
	}

	parts := splitPath(r.URL.Path)
	if len(parts) >= 4 && parts[0] == "api" && parts[1] == "drive" && parts[2] == "uploads" {
		if len(parts) == 4 {
			switch r.Method {
			case http.MethodHead, http.MethodPatch:
				s.handleUploadChunk(w, r, parts[3])
				return
			case http.MethodDelete:
				s.handleDeleteUpload(w, parts[3])
				return
			}
		}
	}
	if len(parts) >= 4 && parts[0] == "api" && parts[1] == "drive" && parts[2] == "transfers" {
		if len(parts) == 4 && r.Method == http.MethodGet {
			s.handleTransferGet(w, parts[3])
			return
		}
		if len(parts) == 5 && r.Method == http.MethodPost {
			s.handleTransferAction(w, parts[3], parts[4])
			return
		}
	}
	if len(parts) >= 4 && parts[0] == "api" && parts[1] == "drive" && parts[2] == "files" {
		fileID := parts[3]
		if len(parts) == 4 && r.Method == http.MethodDelete {
			s.handleDeleteFile(w, r, fileID)
			return
		}
		if len(parts) == 5 && parts[4] == "download" && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
			s.handleDownload(w, r, fileID)
			return
		}
		if len(parts) == 5 && parts[4] == "import" && r.Method == http.MethodPost {
			s.handleImport(w, r, fileID)
			return
		}
	}
	writeError(w, http.StatusNotFound, errors.New("route not found"))
}

func (s *Server) handleListFiles(w http.ResponseWriter, r *http.Request) {
	pageSize, _ := strconv.ParseInt(r.URL.Query().Get("pageSize"), 10, 64)
	files, err := s.drive.List(r.Context(), r.URL.Query().Get("pageToken"), pageSize)
	if err != nil {
		writeDriveError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, files)
}

func (s *Server) handleDeleteFile(w http.ResponseWriter, r *http.Request, fileID string) {
	if err := s.drive.Trash(r.Context(), fileID); err != nil {
		writeDriveError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) handleDownload(w http.ResponseWriter, r *http.Request, fileID string) {
	metadata, response, err := s.drive.DownloadRange(r.Context(), fileID, r.Header.Get("Range"))
	if err != nil {
		writeDriveError(w, err)
		return
	}
	defer response.Body.Close()
	copyHeader(w.Header(), response.Header, "Content-Range", "Content-Length", "Accept-Ranges", "ETag", "Last-Modified")
	if metadata.MimeType != "" {
		w.Header().Set("Content-Type", metadata.MimeType)
	}
	name := filepath.Base(metadata.Name)
	if name != "" && name != "." {
		w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", name))
	}
	w.WriteHeader(response.StatusCode)
	if r.Method == http.MethodHead {
		return
	}
	_, _ = io.Copy(w, response.Body)
}

func (s *Server) handleImport(w http.ResponseWriter, r *http.Request, fileID string) {
	t, err := s.transfers.StartPythonImport(fileID)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusAccepted, t)
}

func (s *Server) handleCreateUpload(w http.ResponseWriter, r *http.Request) {
	// The browser uploads to local disk first. This keeps the request contract
	// independent from Drive latency and lets the worker resume after restart.
	lengthHeader := r.Header.Get("X-Upload-Length")
	if lengthHeader == "" {
		lengthHeader = r.Header.Get("Upload-Length")
	}
	length, err := strconv.ParseInt(lengthHeader, 10, 64)
	if err != nil || length <= 0 {
		writeError(w, http.StatusBadRequest, errors.New("X-Upload-Length 必须是正整数"))
		return
	}
	if length > s.cfg.MaxUploadBytes {
		writeError(w, http.StatusRequestEntityTooLarge, fmt.Errorf("文件超过最大大小 %d bytes", s.cfg.MaxUploadBytes))
		return
	}
	if err := os.MkdirAll(s.cfg.StagingDir(), 0o700); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	name := r.Header.Get("X-File-Name")
	if name == "" {
		name = "upload.bin"
	}
	mime := r.Header.Get("X-File-Mime")
	if mime == "" {
		mime = "application/octet-stream"
	}
	finalPath := filepath.Join(s.cfg.StagingDir(), fmt.Sprintf("upload-%d.part", time.Now().UnixNano()))
	file, err := os.OpenFile(finalPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if err := file.Close(); err != nil {
		_ = os.Remove(finalPath)
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	u, err := s.store.CreateUpload(finalPath, filepath.Base(name), mime, length)
	if err != nil {
		_ = os.Remove(finalPath)
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	w.Header().Set("Location", "/api/drive/uploads/"+u.ID)
	w.Header().Set("Upload-Offset", "0")
	w.Header().Set("Upload-Length", strconv.FormatInt(u.Length, 10))
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": u.ID, "uploadUrl": "/api/drive/uploads/" + u.ID,
		"offset": u.Offset, "length": u.Length, "name": u.Name,
	})
}

func (s *Server) handleUploadChunk(w http.ResponseWriter, r *http.Request, id string) {
	u, ok := s.store.GetUpload(id)
	if !ok {
		writeError(w, http.StatusNotFound, errors.New("upload not found"))
		return
	}
	w.Header().Set("Upload-Length", strconv.FormatInt(u.Length, 10))
	w.Header().Set("Upload-Offset", strconv.FormatInt(u.Offset, 10))
	if r.Method == http.MethodHead {
		if u.Completed {
			w.Header().Set("Upload-Complete", "true")
		}
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if u.Completed {
		writeError(w, http.StatusConflict, errors.New("upload already completed"))
		return
	}
	offsetHeader := r.Header.Get("X-Upload-Offset")
	if offsetHeader == "" {
		offsetHeader = r.Header.Get("Upload-Offset")
	}
	offset, err := strconv.ParseInt(offsetHeader, 10, 64)
	if err != nil || offset != u.Offset {
		w.Header().Set("Upload-Offset", strconv.FormatInt(u.Offset, 10))
		writeError(w, http.StatusConflict, fmt.Errorf("offset mismatch, server=%d", u.Offset))
		return
	}
	// Only the declared remaining bytes may be written; io.CopyN below also
	// prevents a client from accidentally consuming the next request's data.
	if r.ContentLength > u.Length-offset {
		writeError(w, http.StatusRequestEntityTooLarge, errors.New("chunk exceeds upload length"))
		return
	}
	f, err := os.OpenFile(u.Path, os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	defer f.Close()
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	limit := u.Length - offset
	if r.ContentLength >= 0 && r.ContentLength < limit {
		limit = r.ContentLength
	}
	written, err := io.CopyN(f, r.Body, limit)
	if err != nil && !errors.Is(err, io.EOF) {
		writeError(w, http.StatusBadRequest, fmt.Errorf("write upload chunk: %w", err))
		return
	}
	if err := f.Sync(); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	newOffset := offset + written
	completed := newOffset == u.Length
	u, err = s.store.UpdateUpload(id, func(upload *store.Upload) error { upload.Offset = newOffset; upload.Completed = completed; return nil })
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if completed {
		t, transferErr := s.transfers.StartDriveUpload(u)
		if transferErr != nil {
			writeError(w, http.StatusInternalServerError, transferErr)
			return
		}
		w.Header().Set("X-Transfer-ID", t.ID)
	}
	w.Header().Set("Upload-Offset", strconv.FormatInt(newOffset, 10))
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) handleDeleteUpload(w http.ResponseWriter, id string) {
	// A completed staging upload belongs to the Drive worker and cannot be
	// removed through this endpoint; use transfer cancellation instead.
	u, ok := s.store.GetUpload(id)
	if !ok {
		writeError(w, http.StatusNotFound, errors.New("upload not found"))
		return
	}
	if u.Completed {
		writeError(w, http.StatusConflict, errors.New("upload is already queued for Drive transfer"))
		return
	}
	if err := os.Remove(u.Path); err != nil && !errors.Is(err, os.ErrNotExist) {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if err := s.store.DeleteUpload(id); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) handleTransferGet(w http.ResponseWriter, id string) {
	t, ok := s.store.GetTransfer(id)
	if !ok {
		writeError(w, http.StatusNotFound, errors.New("transfer not found"))
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *Server) handleTransferAction(w http.ResponseWriter, id, action string) {
	var err error
	switch action {
	case "pause":
		err = s.transfers.Pause(id)
	case "resume":
		err = s.transfers.Resume(id)
	case "cancel":
		err = s.transfers.Cancel(id)
	default:
		writeError(w, http.StatusNotFound, errors.New("unknown transfer action"))
		return
	}
	if err != nil {
		writeError(w, http.StatusConflict, err)
		return
	}
	t, _ := s.store.GetTransfer(id)
	writeJSON(w, http.StatusOK, t)
}

func (s *Server) setCommonHeaders(w http.ResponseWriter, r *http.Request) {
	if origin := r.Header.Get("Origin"); origin == "http://127.0.0.1:8000" || origin == "http://localhost:8000" {
		// The sidecar is local-only; allow the two local origins used by the
		// Python UI while keeping arbitrary origins out of the CORS response.
		w.Header().Set("Access-Control-Allow-Origin", origin)
	}
	w.Header().Set("Access-Control-Allow-Methods", "GET,POST,PATCH,HEAD,DELETE,OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type,Range,Authorization,X-API-Token,X-Upload-Length,X-Upload-Offset,Upload-Length,Upload-Offset,X-File-Name,X-File-Mime")
	w.Header().Set("Access-Control-Expose-Headers", "Content-Length,Content-Range,Accept-Ranges,ETag,Location,Upload-Offset,Upload-Length,X-Transfer-ID")
	w.Header().Add("Vary", "Origin")
}

func writeDriveError(w http.ResponseWriter, err error) {
	status := http.StatusBadGateway
	message := err.Error()
	if strings.Contains(message, "尚未完成 Google Drive 授权") ||
		strings.Contains(message, "ClientID") ||
		strings.Contains(message, "invalid_grant") ||
		strings.Contains(message, "invalid_token") {
		status = http.StatusServiceUnavailable
	}
	writeError(w, status, err)
}

func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]any{"error": err.Error()})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func copyHeader(dst, src http.Header, keys ...string) {
	for _, key := range keys {
		if value := src.Get(key); value != "" {
			dst.Set(key, value)
		}
	}
}

func splitPath(value string) []string {
	parts := strings.Split(strings.Trim(value, "/"), "/")
	result := make([]string, 0, len(parts))
	for _, part := range parts {
		if part != "" {
			result = append(result, part)
		}
	}
	return result
}
