// Package store persists upload and transfer metadata in one private JSON
// file. Every mutation is serialized and atomically renamed so a restart can
// recover the last acknowledged transfer position.
package store

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// Upload describes a Tus-like local staging upload before it is sent to Drive.
type Upload struct {
	ID        string `json:"id"`
	Path      string `json:"path"`
	Name      string `json:"name"`
	MIME      string `json:"mime"`
	Length    int64  `json:"length"`
	Offset    int64  `json:"offset"`
	Completed bool   `json:"completed"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at"`
}

// Transfer is the durable state-machine record for a Drive upload or Python
// import. LocalPath and SessionURL make resumable work restartable.
type Transfer struct {
	ID           string `json:"id"`
	Kind         string `json:"kind"`
	State        string `json:"state"`
	UploadID     string `json:"upload_id,omitempty"`
	FileID       string `json:"file_id,omitempty"`
	FileName     string `json:"file_name,omitempty"`
	MIME         string `json:"mime,omitempty"`
	LocalPath    string `json:"local_path,omitempty"`
	SessionURL   string `json:"session_url,omitempty"`
	PythonTaskID string `json:"python_task_id,omitempty"`
	TotalBytes   int64  `json:"total_bytes"`
	Transferred  int64  `json:"transferred_bytes"`
	ExpectedMD5  string `json:"expected_md5,omitempty"`
	ETag         string `json:"etag,omitempty"`
	Error        string `json:"error,omitempty"`
	CreatedAt    string `json:"created_at"`
	UpdatedAt    string `json:"updated_at"`
}

type stateFile struct {
	DriveFolderID string              `json:"drive_folder_id,omitempty"`
	Uploads       map[string]Upload   `json:"uploads"`
	Transfers     map[string]Transfer `json:"transfers"`
}

// Store is the concurrency-safe state repository shared by all handlers and
// transfer workers.
type Store struct {
	path string
	mu   sync.Mutex
	data stateFile
}

// Open loads an existing state file or initializes an empty repository.
func Open(path string) (*Store, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("create state directory: %w", err)
	}
	s := &Store{path: path, data: stateFile{Uploads: map[string]Upload{}, Transfers: map[string]Transfer{}}}
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return s, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read state: %w", err)
	}
	if err := json.Unmarshal(b, &s.data); err != nil {
		return nil, fmt.Errorf("parse state: %w", err)
	}
	if s.data.Uploads == nil {
		s.data.Uploads = map[string]Upload{}
	}
	if s.data.Transfers == nil {
		s.data.Transfers = map[string]Transfer{}
	}
	return s, nil
}

func (s *Store) mutate(fn func(*stateFile) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := fn(&s.data); err != nil {
		return err
	}
	return s.saveLocked()
}

func (s *Store) saveLocked() error {
	// Write then rename keeps readers from observing a partially written JSON
	// document if the process is interrupted during persistence.
	b, err := json.MarshalIndent(s.data, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return fmt.Errorf("write state: %w", err)
	}
	if err := os.Rename(tmp, s.path); err != nil {
		return fmt.Errorf("replace state: %w", err)
	}
	return nil
}

// DriveFolderID returns the cached application-folder ID, if known.
func (s *Store) DriveFolderID() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.data.DriveFolderID
}

// SetDriveFolderID persists the Drive folder used by all sidecar files.
func (s *Store) SetDriveFolderID(id string) error {
	return s.mutate(func(d *stateFile) error {
		d.DriveFolderID = id
		return nil
	})
}

// CreateUpload registers a new local staging upload.
func (s *Store) CreateUpload(path, name, mime string, length int64) (Upload, error) {
	u := Upload{ID: newID(), Path: path, Name: name, MIME: mime, Length: length, CreatedAt: now(), UpdatedAt: now()}
	err := s.mutate(func(d *stateFile) error { d.Uploads[u.ID] = u; return nil })
	return u, err
}

// GetUpload returns an upload snapshot without exposing mutable internal state.
func (s *Store) GetUpload(id string) (Upload, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	u, ok := s.data.Uploads[id]
	return u, ok
}

// UpdateUpload applies an atomic mutation and refreshes UpdatedAt.
func (s *Store) UpdateUpload(id string, fn func(*Upload) error) (Upload, error) {
	var out Upload
	err := s.mutate(func(d *stateFile) error {
		u, ok := d.Uploads[id]
		if !ok {
			return fmt.Errorf("upload %s not found", id)
		}
		if err := fn(&u); err != nil {
			return err
		}
		u.UpdatedAt = now()
		d.Uploads[id] = u
		out = u
		return nil
	})
	return out, err
}

// DeleteUpload removes the local staging record.
func (s *Store) DeleteUpload(id string) error {
	return s.mutate(func(d *stateFile) error { delete(d.Uploads, id); return nil })
}

// CreateTransfer persists a new transfer in PENDING state unless supplied.
func (s *Store) CreateTransfer(t Transfer) (Transfer, error) {
	if t.ID == "" {
		t.ID = newID()
	}
	if t.State == "" {
		t.State = "PENDING"
	}
	if t.CreatedAt == "" {
		t.CreatedAt = now()
	}
	t.UpdatedAt = now()
	err := s.mutate(func(d *stateFile) error { d.Transfers[t.ID] = t; return nil })
	return t, err
}

// GetTransfer returns a transfer snapshot.
func (s *Store) GetTransfer(id string) (Transfer, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.data.Transfers[id]
	return t, ok
}

// UpdateTransfer applies an atomic state/progress mutation.
func (s *Store) UpdateTransfer(id string, fn func(*Transfer) error) (Transfer, error) {
	var out Transfer
	err := s.mutate(func(d *stateFile) error {
		t, ok := d.Transfers[id]
		if !ok {
			return fmt.Errorf("transfer %s not found", id)
		}
		if err := fn(&t); err != nil {
			return err
		}
		t.UpdatedAt = now()
		d.Transfers[id] = t
		out = t
		return nil
	})
	return out, err
}

// ListTransfers returns transfers newest-updated first.
func (s *Store) ListTransfers() []Transfer {
	s.mu.Lock()
	defer s.mu.Unlock()
	result := make([]Transfer, 0, len(s.data.Transfers))
	for _, t := range s.data.Transfers {
		result = append(result, t)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].UpdatedAt > result[j].UpdatedAt })
	return result
}

// RecoverableTransfers selects non-terminal transfers that can be restarted
// safely when the process boots again.
func (s *Store) RecoverableTransfers() []Transfer {
	all := s.ListTransfers()
	result := make([]Transfer, 0, len(all))
	for _, t := range all {
		if t.State == "PENDING" || t.State == "TRANSFERRING" || t.State == "RETRYING" || t.State == "VERIFYING" {
			result = append(result, t)
		}
	}
	return result
}

func newID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("fallback-%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b)
}

func now() string { return time.Now().UTC().Format(time.RFC3339Nano) }
