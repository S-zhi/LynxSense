package store

import (
	"path/filepath"
	"testing"
)

// TestStorePersistsUploadsAndTransfers 验证上传、传输状态以及可恢复任务的持久化。
func TestStorePersistsUploadsAndTransfers(t *testing.T) {
	t.Parallel()
	statePath := filepath.Join(t.TempDir(), "nested", "state.json")
	s, err := Open(statePath)
	if err != nil {
		t.Fatal(err)
	}
	upload, err := s.CreateUpload("/tmp/video.part", "video.mp4", "video/mp4", 10)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.UpdateUpload(upload.ID, func(u *Upload) error {
		u.Offset = 3
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	transfer, err := s.CreateTransfer(Transfer{
		Kind:     "DRIVE_UPLOAD",
		State:    "TRANSFERRING",
		UploadID: upload.ID,
	})
	if err != nil {
		t.Fatal(err)
	}

	reopened, err := Open(statePath)
	if err != nil {
		t.Fatal(err)
	}
	gotUpload, ok := reopened.GetUpload(upload.ID)
	if !ok || gotUpload.Offset != 3 {
		t.Fatalf("persisted upload = %#v, present=%v", gotUpload, ok)
	}
	gotTransfer, ok := reopened.GetTransfer(transfer.ID)
	if !ok || gotTransfer.State != "TRANSFERRING" {
		t.Fatalf("persisted transfer = %#v, present=%v", gotTransfer, ok)
	}
	recoverable := reopened.RecoverableTransfers()
	if len(recoverable) != 1 || recoverable[0].ID != transfer.ID {
		t.Fatalf("recoverable transfers = %#v", recoverable)
	}
}

// TestFolderBatchIdempotencyAndPersistence verifies that a client retry gets
// the original batch and that its entries survive a process restart.
func TestFolderBatchIdempotencyAndPersistence(t *testing.T) {
	t.Parallel()
	statePath := filepath.Join(t.TempDir(), "state.json")
	s, err := Open(statePath)
	if err != nil {
		t.Fatal(err)
	}
	batch, existing, err := s.CreateFolderBatch("request-1", "parent", 1, 42)
	if err != nil || existing {
		t.Fatalf("create batch = %#v existing=%v err=%v", batch, existing, err)
	}
	retry, existing, err := s.CreateFolderBatch("request-1", "different-parent", 99, 99)
	if err != nil || !existing || retry.ID != batch.ID {
		t.Fatalf("idempotent retry = %#v existing=%v err=%v", retry, existing, err)
	}
	entry, err := s.CreateFolderEntry(FolderEntry{
		BatchID: batch.ID, RelativePath: "dir/video.mp4", Name: "video.mp4",
		MIME: "video/mp4", Size: 42, ParentID: "folder", State: "PENDING",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.UpdateFolderEntry(entry.ID, func(e *FolderEntry) error {
		e.State = "SUCCESS"
		e.FileID = "file-1"
		return nil
	}); err != nil {
		t.Fatal(err)
	}

	reopened, err := Open(statePath)
	if err != nil {
		t.Fatal(err)
	}
	gotBatch, ok := reopened.GetFolderBatchByClientRequestID("request-1")
	if !ok || gotBatch.ID != batch.ID {
		t.Fatalf("persisted batch = %#v present=%v", gotBatch, ok)
	}
	entries := reopened.ListFolderEntries(batch.ID)
	if len(entries) != 1 || entries[0].State != "SUCCESS" || entries[0].FileID != "file-1" {
		t.Fatalf("persisted entries = %#v", entries)
	}
}
