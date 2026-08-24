package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

const defaultScope = "https://www.googleapis.com/auth/drive.file"

const driveChunkMultiple = int64(256 * 1024)

// Config contains the small amount of process configuration needed by the
// single-user Drive sidecar. Credentials are intentionally empty in the
// example config and can be supplied through a local OAuth client JSON file.
type Config struct {
	ListenAddr             string   `json:"listen_addr"`
	DataDir                string   `json:"data_dir"`
	PythonBaseURL          string   `json:"python_base_url"`
	PythonAPIToken         string   `json:"python_api_token"`
	GoogleClientID         string   `json:"google_client_id"`
	GoogleClientSecret     string   `json:"google_client_secret"`
	GoogleRedirectURI      string   `json:"google_redirect_uri"`
	GoogleClientConfigFile string   `json:"google_client_config_file"`
	GoogleScopes           []string `json:"google_scopes"`
	DriveFolderID          string   `json:"drive_folder_id"`
	MaxUploadBytes         int64    `json:"max_upload_bytes"`
	ChunkSizeBytes         int64    `json:"chunk_size_bytes"`
	RequestTimeoutSeconds  int      `json:"request_timeout_seconds"`
}

func Default() Config {
	return Config{
		ListenAddr:            "127.0.0.1:8787",
		DataDir:               "./drive-data",
		PythonBaseURL:         "http://127.0.0.1:8000",
		GoogleScopes:          []string{defaultScope},
		MaxUploadBytes:        2 * 1024 * 1024 * 1024,
		ChunkSizeBytes:        16 * 1024 * 1024,
		RequestTimeoutSeconds: 600,
	}
}

func Load(path string) (Config, error) {
	cfg := Default()
	if path == "" {
		return cfg, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("read config %s: %w", path, err)
	}
	if err := json.Unmarshal(b, &cfg); err != nil {
		return cfg, fmt.Errorf("parse config %s: %w", path, err)
	}
	if cfg.ListenAddr == "" {
		cfg.ListenAddr = Default().ListenAddr
	}
	if cfg.DataDir == "" {
		cfg.DataDir = Default().DataDir
	}
	if cfg.PythonBaseURL == "" {
		cfg.PythonBaseURL = Default().PythonBaseURL
	}
	if len(cfg.GoogleScopes) == 0 {
		cfg.GoogleScopes = []string{defaultScope}
	}
	if cfg.MaxUploadBytes <= 0 {
		cfg.MaxUploadBytes = Default().MaxUploadBytes
	}
	if cfg.ChunkSizeBytes <= 0 {
		cfg.ChunkSizeBytes = Default().ChunkSizeBytes
	}
	// Drive resumable uploads require every non-final chunk to be a multiple
	// of 256 KiB. Normalize an accidental smaller/non-aligned local setting so
	// a config typo cannot turn into a remote 400 response halfway through a
	// large upload.
	if cfg.ChunkSizeBytes < driveChunkMultiple {
		cfg.ChunkSizeBytes = driveChunkMultiple
	} else {
		cfg.ChunkSizeBytes -= cfg.ChunkSizeBytes % driveChunkMultiple
		if cfg.ChunkSizeBytes < driveChunkMultiple {
			cfg.ChunkSizeBytes = driveChunkMultiple
		}
	}
	if cfg.RequestTimeoutSeconds <= 0 {
		cfg.RequestTimeoutSeconds = Default().RequestTimeoutSeconds
	}
	return cfg, nil
}

func (c Config) EnsureDataDir() error {
	if c.DataDir == "" {
		return errors.New("data_dir is empty")
	}
	return os.MkdirAll(filepath.Clean(c.DataDir), 0o700)
}

func (c Config) StatePath() string {
	return filepath.Join(c.DataDir, "state.json")
}

func (c Config) TokenPath() string {
	return filepath.Join(c.DataDir, "oauth_token.json")
}

func (c Config) StagingDir() string {
	return filepath.Join(c.DataDir, "staging")
}

func (c Config) ClientConfigPath() string {
	if c.GoogleClientConfigFile != "" {
		return c.GoogleClientConfigFile
	}
	return filepath.Join(c.DataDir, "oauth_client.json")
}
