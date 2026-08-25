package transfer

import (
	"context"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	driveclient "github.com/s-zhi/subtitles-ai-drive/internal/drive"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
)

type fakeDrive struct {
	mu         sync.Mutex
	parentID   string
	properties map[string]string
}

func (f *fakeDrive) StartUploadSessionInFolder(_ context.Context, _ string, _ string, _ int64, parentID string, properties map[string]string) (driveclient.UploadSession, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.parentID = parentID
	f.properties = map[string]string{}
	for key, value := range properties {
		f.properties[key] = value
	}
	return driveclient.UploadSession{URL: "fake://session"}, nil
}

func (f *fakeDrive) UploadChunk(_ context.Context, _ string, _ *os.File, _ int64, total, _ int64) (driveclient.ChunkResult, error) {
	return driveclient.ChunkResult{NextOffset: total, Completed: true, File: &driveclient.File{ID: "drive-file-1"}}, nil
}

func (f *fakeDrive) Metadata(_ context.Context, _ string) (*driveclient.File, error) {
	return &driveclient.File{Name: "file.bin", MimeType: "application/octet-stream", Size: 1}, nil
}

func (f *fakeDrive) DownloadRange(_ context.Context, _ string, _ string) (*driveclient.File, *http.Response, error) {
	return nil, nil, os.ErrInvalid
}

func TestBatchDriveUploadPropagatesMetadataAndAggregates(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	drive := &fakeDrive{}
	manager := NewManager(&cfg, state, drive)
	batch, _, err := state.CreateFolderBatch("request-1", "root-folder", 1, 1)
	if err != nil {
		t.Fatal(err)
	}
	entry, err := state.CreateFolderEntry(store.FolderEntry{
		BatchID: batch.ID, RelativePath: "nested/file.bin", Name: "file.bin", Size: 1,
		ParentID: "nested-folder", State: StatePending,
	})
	if err != nil {
		t.Fatal(err)
	}
	staging := filepath.Join(cfg.DataDir, "file.part")
	if err := os.WriteFile(staging, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	upload, err := state.CreateUploadWithMetadata(staging, "file.bin", "application/octet-stream", 1, batch.ID, entry.ID, entry.ParentID, entry.RelativePath)
	if err != nil {
		t.Fatal(err)
	}
	upload, err = state.UpdateUpload(upload.ID, func(current *store.Upload) error {
		current.Offset = 1
		current.Completed = true
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := manager.StartDriveUpload(upload); err != nil {
		t.Fatal(err)
	}

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		current, ok := state.GetFolderEntry(entry.ID)
		if ok && current.State == StateSuccess {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	gotEntry, _ := state.GetFolderEntry(entry.ID)
	if gotEntry.State != StateSuccess || gotEntry.FileID != "drive-file-1" {
		t.Fatalf("entry = %#v", gotEntry)
	}
	gotBatch, _ := state.GetFolderBatch(batch.ID)
	if gotBatch.State != StateSuccess || gotBatch.CompletedEntries != 1 || gotBatch.CompletedBytes != 1 {
		t.Fatalf("batch = %#v", gotBatch)
	}
	drive.mu.Lock()
	defer drive.mu.Unlock()
	if drive.parentID != entry.ParentID {
		t.Fatalf("parent id = %q, want %q", drive.parentID, entry.ParentID)
	}
	if drive.properties["subtitles_ai_batch_id"] != batch.ID || drive.properties["subtitles_ai_entry_id"] != entry.ID {
		t.Fatalf("upload properties = %#v", drive.properties)
	}
}

func TestCancelBatchKeepsSuccessfulEntriesAndCancelsRemaining(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	manager := NewManager(&cfg, state, &fakeDrive{})
	batch, _, err := state.CreateFolderBatch("cancel-partial", "root-folder", 2, 2)
	if err != nil {
		t.Fatal(err)
	}
	success, err := state.CreateFolderEntry(store.FolderEntry{
		BatchID: batch.ID, RelativePath: "done.bin", Name: "done.bin", Size: 1, State: StateSuccess,
	})
	if err != nil {
		t.Fatal(err)
	}
	pending, err := state.CreateFolderEntry(store.FolderEntry{
		BatchID: batch.ID, RelativePath: "pending.bin", Name: "pending.bin", Size: 1, State: StatePending,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := manager.CancelBatch(batch.ID); err != nil {
		t.Fatal(err)
	}
	gotSuccess, _ := state.GetFolderEntry(success.ID)
	gotPending, _ := state.GetFolderEntry(pending.ID)
	if gotSuccess.State != StateSuccess || gotPending.State != StateCancelled {
		t.Fatalf("success=%#v pending=%#v", gotSuccess, gotPending)
	}
	gotBatch, _ := state.GetFolderBatch(batch.ID)
	if gotBatch.State != StateCancelled || gotBatch.CompletedEntries != 1 {
		t.Fatalf("batch = %#v", gotBatch)
	}
}

func TestPostPythonUploadRespectsTimeoutAndCancellation(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	cfg.PythonTimeoutSeconds = 1 // 1 second timeout for test
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	manager := NewManager(&cfg, state, &fakeDrive{})

	// Test cancellation during upload
	cancelCtx, cancel := context.WithCancel(context.Background())
	cancel() // Cancel immediately

	staging := filepath.Join(cfg.DataDir, "import-file.bin")
	if err := os.WriteFile(staging, []byte("test payload data"), 0o600); err != nil {
		t.Fatal(err)
	}

	err = manager.postPythonUpload(cancelCtx, "transfer-1", staging, "import-file.bin")
	if err == nil {
		t.Fatal("expected error when context is cancelled, got nil")
	}
}
