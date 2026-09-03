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

// TestStoreUpdateTransferProgressThrottling 验证 UpdateTransferProgress 的内存更新、节流落盘以及 Flush 强制落盘。
func TestStoreUpdateTransferProgressThrottling(t *testing.T) {
	t.Parallel()
	statePath := filepath.Join(t.TempDir(), "state.json")
	s, err := Open(statePath)
	if err != nil {
		t.Fatal(err)
	}
	tRecord, err := s.CreateTransfer(Transfer{
		Kind:       "PYTHON_IMPORT",
		State:      "TRANSFERRING",
		TotalBytes: 1000,
	})
	if err != nil {
		t.Fatal(err)
	}

	// UpdateTransferProgress 在创建后立即调用 (未满 1 秒)，只更新内存，磁盘仍为 0
	if _, err := s.UpdateTransferProgress(tRecord.ID, 100); err != nil {
		t.Fatal(err)
	}
	if inMem, ok := s.GetTransfer(tRecord.ID); !ok || inMem.Transferred != 100 {
		t.Fatalf("in-memory transfer progress expected 100, got %d", inMem.Transferred)
	}
	reopened, err := Open(statePath)
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := reopened.GetTransfer(tRecord.ID); got.Transferred != 0 {
		t.Fatalf("expected throttled disk progress to be 0, got %d", got.Transferred)
	}

	// 调用 Flush 强制落盘
	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}
	reopened2, err := Open(statePath)
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := reopened2.GetTransfer(tRecord.ID); got.Transferred != 100 {
		t.Fatalf("expected flushed disk progress to be 100, got %d", got.Transferred)
	}
}

// TestRecoverableTransfersExcludesPausedAndTerminalStates 验证 RecoverableTransfers 仅返回 PENDING, TRANSFERRING, RETRYING, VERIFYING，
// 排除 PAUSED 以及终态任务 (SUCCESS, FAILED, CANCELLED)。
func TestRecoverableTransfersExcludesPausedAndTerminalStates(t *testing.T) {
	t.Parallel()
	statePath := filepath.Join(t.TempDir(), "state.json")
	s, err := Open(statePath)
	if err != nil {
		t.Fatal(err)
	}

	states := map[string]bool{
		"PENDING":      true,
		"TRANSFERRING": true,
		"RETRYING":     true,
		"VERIFYING":    true,
		"PAUSED":       false,
		"SUCCESS":      false,
		"FAILED":       false,
		"CANCELLED":    false,
	}

	for state := range states {
		if _, err := s.CreateTransfer(Transfer{
			Kind:  "DRIVE_UPLOAD",
			State: state,
		}); err != nil {
			t.Fatal(err)
		}
	}

	recoverable := s.RecoverableTransfers()
	if len(recoverable) != 4 {
		t.Fatalf("expected 4 recoverable transfers, got %d", len(recoverable))
	}

	for _, tr := range recoverable {
		if !states[tr.State] {
			t.Errorf("unexpected recoverable state: %s", tr.State)
		}
	}
}
