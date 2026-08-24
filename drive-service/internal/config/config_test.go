package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestLoadAppliesSafeDefaultsAndDriveChunkAlignment(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	configPath := filepath.Join(tmp, "config.json")
	payload, err := json.Marshal(map[string]any{
		"data_dir":         filepath.Join(tmp, "data"),
		"chunk_size_bytes": 300000,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(configPath, payload, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ChunkSizeBytes != driveChunkMultiple {
		t.Fatalf("chunk size = %d, want %d", cfg.ChunkSizeBytes, driveChunkMultiple)
	}
	if len(cfg.GoogleScopes) != 1 || cfg.GoogleScopes[0] != defaultScope {
		t.Fatalf("unexpected scopes: %#v", cfg.GoogleScopes)
	}
	if err := cfg.EnsureDataDir(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(cfg.StatePath()); !os.IsNotExist(err) {
		t.Fatalf("state path should not be created by EnsureDataDir: %v", err)
	}
}
