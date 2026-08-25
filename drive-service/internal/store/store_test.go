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
