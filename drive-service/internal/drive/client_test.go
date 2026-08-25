package drive

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	"github.com/s-zhi/subtitles-ai-drive/internal/oauth"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
	"golang.org/x/oauth2"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// newTestClient supplies a valid local OAuth token and replaces only the
// underlying HTTP transport. This keeps client tests offline while exercising
// the same authenticated request path used in production.
func newTestClient(t *testing.T, transport http.RoundTripper) (*Client, *config.Config, *store.Store, context.Context) {
	t.Helper()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	cfg.GoogleClientID = "test-client-id"
	cfg.GoogleClientSecret = "test-client-secret"
	if err := cfg.EnsureDataDir(); err != nil {
		t.Fatal(err)
	}
	token, err := json.Marshal(&oauth2.Token{AccessToken: "test-access-token", RefreshToken: "test-refresh-token", TokenType: "Bearer"})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cfg.TokenPath(), token, 0o600); err != nil {
		t.Fatal(err)
	}
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	client := NewClient(&cfg, oauth.NewManager(&cfg), state)
	httpClient := &http.Client{Transport: transport}
	ctx := context.WithValue(context.Background(), oauth2.HTTPClient, httpClient)
	return client, &cfg, state, ctx
}

func responseJSON(status int, value any) *http.Response {
	body, _ := json.Marshal(value)
	return &http.Response{
		StatusCode:    status,
		Status:        http.StatusText(status),
		Header:        make(http.Header),
		Body:          io.NopCloser(bytes.NewReader(body)),
		ContentLength: int64(len(body)),
	}
}

func TestIsFolder(t *testing.T) {
	t.Parallel()
	if !IsFolder(&File{MimeType: folderMIME}) {
		t.Fatal("folder MIME type was not recognized")
	}
	if IsFolder(&File{MimeType: "video/mp4"}) {
		t.Fatal("a regular file was recognized as a folder")
	}
	if IsFolder(nil) {
		t.Fatal("nil metadata was recognized as a folder")
	}
}

func TestCreateFolderUsesExplicitParentAndProperties(t *testing.T) {
	var mu sync.Mutex
	var gotBody map[string]any
	var gotURL *url.URL
	client, _, _, ctx := newTestClient(t, roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost {
			t.Fatalf("method = %s, want POST", r.Method)
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatal(err)
		}
		mu.Lock()
		defer mu.Unlock()
		if err := json.Unmarshal(body, &gotBody); err != nil {
			t.Fatal(err)
		}
		gotURL = r.URL
		return responseJSON(http.StatusOK, map[string]any{
			"id": "folder-1", "name": "new-folder", "mimeType": folderMIME,
			"parents": []string{"root-1"}, "appProperties": map[string]string{
				appPropertyKey: "true", "batch": "b1",
			},
		}), nil
	}))

	folder, err := client.CreateFolder(ctx, "nested/new-folder", "root-1", map[string]string{"batch": "b1", appPropertyKey: "false"})
	if err != nil {
		t.Fatal(err)
	}
	if folder.ID != "folder-1" || !IsFolder(folder) {
		t.Fatalf("folder = %#v", folder)
	}
	if gotURL == nil || gotURL.Path != "/drive/v3/files" || gotURL.Query().Get("supportsAllDrives") != "true" {
		t.Fatalf("request URL = %v", gotURL)
	}
	if gotBody["name"] != "new-folder" || gotBody["mimeType"] != folderMIME {
		t.Fatalf("metadata = %#v", gotBody)
	}
	parents, ok := gotBody["parents"].([]any)
	if !ok || len(parents) != 1 || parents[0] != "root-1" {
		t.Fatalf("parents = %#v", gotBody["parents"])
	}
	properties, ok := gotBody["appProperties"].(map[string]any)
	if !ok || properties["batch"] != "b1" || properties[appPropertyKey] != "true" {
		t.Fatalf("appProperties = %#v", gotBody["appProperties"])
	}
}

func TestCreateFolderEmptyParentResolvesRoot(t *testing.T) {
	var calls int
	client, cfg, _, ctx := newTestClient(t, roundTripFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.Method != http.MethodPost {
			t.Fatalf("method = %s, want POST", r.Method)
		}
		return responseJSON(http.StatusOK, map[string]any{
			"id": "folder-2", "name": "top", "mimeType": folderMIME,
			"parents": []string{"root-2"},
		}), nil
	}))
	cfg.DriveFolderID = "root-2"

	folder, err := client.CreateFolder(ctx, "top", "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 1 || folder.ID != "folder-2" {
		t.Fatalf("calls=%d folder=%#v", calls, folder)
	}
}

func TestEnsureFolderUsesMarkerAndFallsBackToLegacyName(t *testing.T) {
	t.Run("legacy", func(t *testing.T) {
		var queries []string
		client, _, state, ctx := newTestClient(t, roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodGet {
				t.Fatalf("method = %s, want GET", r.Method)
			}
			queries = append(queries, r.URL.Query().Get("q"))
			if len(queries) == 1 {
				return responseJSON(http.StatusOK, map[string]any{"files": []any{}}), nil
			}
			return responseJSON(http.StatusOK, map[string]any{"files": []map[string]any{{
				"id": "legacy-root", "name": rootFolderName, "mimeType": folderMIME,
			}}}), nil
		}))
		id, err := client.ensureFolder(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if id != "legacy-root" || state.DriveFolderID() != id {
			t.Fatalf("id=%q cached=%q", id, state.DriveFolderID())
		}
		if len(queries) != 2 || !strings.Contains(queries[0], "appProperties has") || !strings.Contains(queries[1], "name = 'Subtitles AI'") {
			t.Fatalf("queries = %#v", queries)
		}
	})

	t.Run("marker", func(t *testing.T) {
		var requests []string
		var created map[string]any
		client, _, state, ctx := newTestClient(t, roundTripFunc(func(r *http.Request) (*http.Response, error) {
			requests = append(requests, r.Method)
			if r.Method == http.MethodGet {
				return responseJSON(http.StatusOK, map[string]any{"files": []any{}}), nil
			}
			body, err := io.ReadAll(r.Body)
			if err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(body, &created); err != nil {
				t.Fatal(err)
			}
			return responseJSON(http.StatusOK, map[string]any{
				"id": "new-root", "name": rootFolderName, "mimeType": folderMIME,
			}), nil
		}))
		id, err := client.ensureFolder(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if id != "new-root" || state.DriveFolderID() != id {
			t.Fatalf("id=%q cached=%q", id, state.DriveFolderID())
		}
		if len(requests) != 3 || requests[0] != http.MethodGet || requests[1] != http.MethodGet || requests[2] != http.MethodPost {
			t.Fatalf("requests = %#v", requests)
		}
		properties, ok := created["appProperties"].(map[string]any)
		if !ok || properties[rootPropertyKey] != "true" || properties[appPropertyKey] != "true" {
			t.Fatalf("root appProperties = %#v", created["appProperties"])
		}
	})
}

func TestListUsesExplicitParentAndPagination(t *testing.T) {
	var gotQuery url.Values
	client, cfg, _, ctx := newTestClient(t, roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodGet {
			t.Fatalf("method = %s, want GET", r.Method)
		}
		if strings.Contains(r.URL.Path, "/files/folder%27id") || strings.HasSuffix(r.URL.Path, "/files/folder'id") {
			return responseJSON(http.StatusOK, File{ID: "folder'id", Name: "child", MimeType: folderMIME, Parents: []string{"root"}}), nil
		}
		gotQuery = r.URL.Query()
		return responseJSON(http.StatusOK, map[string]any{
			"nextPageToken": "next", "files": []map[string]any{{
				"id": "file-1", "name": "video.mp4", "mimeType": "video/mp4", "size": "4",
				"appProperties": map[string]string{"batch": "b1"},
			}},
		}), nil
	}))
	cfg.DriveFolderID = "root"
	result, err := client.List(ctx, "folder'id", "page-2", 25)
	if err != nil {
		t.Fatal(err)
	}
	if result.NextPageToken != "next" || len(result.Files) != 1 || result.Files[0].AppProperties["batch"] != "b1" {
		t.Fatalf("result = %#v", result)
	}
	if result.RootFolderID != "root" || result.CurrentFolder == nil || result.CurrentFolder.ID != "folder'id" {
		t.Fatalf("folder context = %#v", result)
	}
	if got := gotQuery.Get("q"); got != "'folder\\'id' in parents and trashed = false" {
		t.Fatalf("q = %q", got)
	}
	if gotQuery.Get("pageToken") != "page-2" || gotQuery.Get("pageSize") != "25" {
		t.Fatalf("pagination query = %v", gotQuery)
	}
}

func TestListEmptyParentUsesConfiguredRoot(t *testing.T) {
	var gotQuery string
	client, cfg, _, ctx := newTestClient(t, roundTripFunc(func(r *http.Request) (*http.Response, error) {
		gotQuery = r.URL.Query().Get("q")
		return responseJSON(http.StatusOK, map[string]any{"files": []any{}}), nil
	}))
	cfg.DriveFolderID = "configured-root"
	result, err := client.List(ctx, "", "", 0)
	if err != nil {
		t.Fatal(err)
	}
	if result.RootFolderID != "configured-root" || result.CurrentFolder == nil || result.CurrentFolder.ID != "configured-root" {
		t.Fatalf("folder context = %#v", result)
	}
	if gotQuery != "'configured-root' in parents and trashed = false" {
		t.Fatalf("q = %q", gotQuery)
	}
}

func TestValidateFolderUnderRoot(t *testing.T) {
	tests := []struct {
		name    string
		files   map[string]File
		target  string
		wantID  string
		wantErr string
	}{
		{
			name:   "direct child",
			target: "child",
			files:  map[string]File{"child": {ID: "child", MimeType: folderMIME, Parents: []string{"root"}}},
			wantID: "child",
		},
		{
			name:   "nested child",
			target: "leaf",
			files: map[string]File{
				"leaf":   {ID: "leaf", MimeType: folderMIME, Parents: []string{"middle"}},
				"middle": {ID: "middle", MimeType: folderMIME, Parents: []string{"root"}},
			},
			wantID: "leaf",
		},
		{
			name:   "multiple parents",
			target: "leaf",
			files:  map[string]File{"leaf": {ID: "leaf", MimeType: folderMIME, Parents: []string{"outside", "root"}}},
			wantID: "leaf",
		},
		{
			name:   "outside root",
			target: "outside",
			files: map[string]File{
				"outside":    {ID: "outside", MimeType: folderMIME, Parents: []string{"other-root"}},
				"other-root": {ID: "other-root", MimeType: folderMIME},
			},
			wantErr: "outside the sidecar root",
		},
		{
			name:    "regular file",
			target:  "file",
			files:   map[string]File{"file": {ID: "file", MimeType: "video/mp4", Parents: []string{"root"}}},
			wantErr: "not a folder",
		},
		{
			name:    "trashed folder",
			target:  "trash",
			files:   map[string]File{"trash": {ID: "trash", MimeType: folderMIME, Trashed: true, Parents: []string{"root"}}},
			wantErr: "in the trash",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			client, cfg, _, ctx := newTestClient(t, roundTripFunc(func(r *http.Request) (*http.Response, error) {
				id := filepath.Base(r.URL.Path)
				file, ok := test.files[id]
				if !ok {
					return responseJSON(http.StatusNotFound, map[string]string{"error": "missing"}), nil
				}
				return responseJSON(http.StatusOK, file), nil
			}))
			cfg.DriveFolderID = "root"
			got, err := client.ValidateFolderUnderRoot(ctx, test.target)
			if test.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), test.wantErr) {
					t.Fatalf("error = %v, want substring %q", err, test.wantErr)
				}
				return
			}
			if err != nil || got == nil || got.ID != test.wantID {
				t.Fatalf("folder=%#v err=%v", got, err)
			}
			resolved, err := client.ResolveTargetFolderID(ctx, test.target)
			if err != nil || resolved != test.wantID {
				t.Fatalf("resolved=%q err=%v", resolved, err)
			}
		})
	}

	t.Run("root itself is rejected", func(t *testing.T) {
		client, cfg, _, ctx := newTestClient(t, roundTripFunc(func(*http.Request) (*http.Response, error) {
			t.Fatal("root validation should not fetch metadata")
			return nil, nil
		}))
		cfg.DriveFolderID = "root"
		if _, err := client.ValidateFolderUnderRoot(ctx, "root"); err == nil || !strings.Contains(err.Error(), "root folder") {
			t.Fatalf("error = %v", err)
		}
	})
}

func TestStartUploadSessionUsesParentAndAppProperties(t *testing.T) {
	var gotBody map[string]any
	var gotHeader http.Header
	client, _, _, ctx := newTestClient(t, roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost || r.URL.Path != "/upload/drive/v3/files" {
			t.Fatalf("request = %s %s", r.Method, r.URL)
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(body, &gotBody); err != nil {
			t.Fatal(err)
		}
		gotHeader = r.Header.Clone()
		resp := responseJSON(http.StatusOK, map[string]any{})
		resp.Header.Set("Location", "https://upload.example/session/1")
		return resp, nil
	}))
	session, err := client.StartUploadSessionInFolder(ctx, "nested/video.mp4", "video/mp4", 123, "folder-9", map[string]string{"batch": "b9", appPropertyKey: "false"})
	if err != nil {
		t.Fatal(err)
	}
	if session.URL != "https://upload.example/session/1" {
		t.Fatalf("session URL = %q", session.URL)
	}
	if gotBody["name"] != "video.mp4" || gotBody["mimeType"] != "video/mp4" {
		t.Fatalf("metadata = %#v", gotBody)
	}
	parents, ok := gotBody["parents"].([]any)
	if !ok || len(parents) != 1 || parents[0] != "folder-9" {
		t.Fatalf("parents = %#v", gotBody["parents"])
	}
	properties, ok := gotBody["appProperties"].(map[string]any)
	if !ok || properties["batch"] != "b9" || properties[appPropertyKey] != "true" {
		t.Fatalf("appProperties = %#v", gotBody["appProperties"])
	}
	if gotHeader.Get("X-Upload-Content-Type") != "video/mp4" || gotHeader.Get("X-Upload-Content-Length") != "123" {
		t.Fatalf("upload headers = %v", gotHeader)
	}
}

func TestStartUploadSessionEmptyParentUsesRootAndRejectsInvalidInput(t *testing.T) {
	var gotBody map[string]any
	client, cfg, _, ctx := newTestClient(t, roundTripFunc(func(r *http.Request) (*http.Response, error) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(body, &gotBody); err != nil {
			t.Fatal(err)
		}
		resp := responseJSON(http.StatusOK, nil)
		resp.Header.Set("Location", "https://upload.example/session/2")
		return resp, nil
	}))
	cfg.DriveFolderID = "configured-root"
	if _, err := client.StartUploadSessionInFolder(ctx, "video.mp4", "", 1, "", nil); err != nil {
		t.Fatal(err)
	}
	parents, ok := gotBody["parents"].([]any)
	if !ok || len(parents) != 1 || parents[0] != "configured-root" {
		t.Fatalf("parents = %#v", gotBody["parents"])
	}
	if gotBody["mimeType"] != "application/octet-stream" {
		t.Fatalf("mimeType = %#v", gotBody["mimeType"])
	}
	if _, err := client.StartUploadSessionInFolder(ctx, "video.mp4", "video/mp4", 0, "configured-root", nil); err == nil {
		t.Fatal("zero-sized upload was accepted")
	}
	if _, err := client.StartUploadSessionInFolder(ctx, "", "video/mp4", 1, "configured-root", nil); err == nil {
		t.Fatal("empty file name was accepted")
	}
}

func TestParseDriveRange(t *testing.T) {
	t.Parallel()
	tests := []struct {
		input string
		want  int64
	}{
		{input: "bytes=0-0", want: 1},
		{input: "bytes=0-16777215", want: 16777216},
		{input: "", want: 0},
		{input: "invalid", want: 0},
		{input: "bytes=1-nope", want: 0},
	}
	for _, test := range tests {
		if got := parseDriveRange(test.input); got != test.want {
			t.Errorf("parseDriveRange(%q) = %d, want %d", test.input, got, test.want)
		}
	}
}

func TestEscapeQueryLiteral(t *testing.T) {
	if got := escapeQueryLiteral("a'b"); got != "a\\'b" {
		t.Fatalf("escaped query literal = %q", got)
	}
}
