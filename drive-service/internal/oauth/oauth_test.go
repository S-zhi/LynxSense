package oauth

import (
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
)

// TestStatusCanPickUpOAuthClientJSONAfterInitialEmptyConfig 验证运行期间补充 OAuth
// 客户端 JSON 后，状态接口可以重新识别配置。
func TestStatusCanPickUpOAuthClientJSONAfterInitialEmptyConfig(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	if err := cfg.EnsureDataDir(); err != nil {
		t.Fatal(err)
	}
	manager := NewManager(&cfg)
	first := manager.Status()
	if first["configured"] != false || first["connected"] != false {
		t.Fatalf("initial status = %#v", first)
	}
	clientJSON := map[string]any{
		"installed": map[string]any{
			"client_id":     "test-client-id",
			"client_secret": "test-client-secret",
		},
	}
	b, err := json.Marshal(clientJSON)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cfg.DataDir, "oauth_client.json"), b, 0o600); err != nil {
		t.Fatal(err)
	}
	second := manager.Status()
	if second["configured"] != true || second["connected"] != false {
		t.Fatalf("status after client config = %#v", second)
	}
	if second["reauthorize"] != true {
		t.Fatalf("missing token should request authorization: %#v", second)
	}
}

func TestStartReleasesLoopbackListenerAfterTimeout(t *testing.T) {
	cfg := config.Default()
	cfg.GoogleClientID = "test-client-id"
	cfg.GoogleClientSecret = "test-client-secret"
	manager := NewManager(&cfg)
	manager.pendingTimeout = 20 * time.Millisecond

	if _, err := manager.Start(); err != nil {
		t.Fatal(err)
	}
	manager.mu.Lock()
	listener := manager.pending.listener
	manager.mu.Unlock()
	waitFor(t, func() bool {
		manager.mu.Lock()
		defer manager.mu.Unlock()
		return manager.pending == nil
	})
	if _, err := net.DialTimeout("tcp", listener.Addr().String(), 100*time.Millisecond); err == nil {
		t.Fatal("OAuth loopback listener is still accepting connections after timeout")
	}
}

func TestStaleOAuthTimeoutDoesNotClearNewSession(t *testing.T) {
	cfg := config.Default()
	cfg.GoogleClientID = "test-client-id"
	cfg.GoogleClientSecret = "test-client-secret"
	manager := NewManager(&cfg)
	manager.pendingTimeout = time.Hour

	if _, err := manager.Start(); err != nil {
		t.Fatal(err)
	}
	manager.mu.Lock()
	first := manager.pending
	manager.mu.Unlock()
	if _, err := manager.Start(); err != nil {
		t.Fatal(err)
	}
	manager.mu.Lock()
	second := manager.pending
	manager.mu.Unlock()
	manager.clearPending(first)
	manager.mu.Lock()
	stillCurrent := manager.pending == second
	manager.mu.Unlock()
	if !stillCurrent {
		t.Fatal("stale OAuth cleanup cleared the newer session")
	}
	manager.clearPending(second)
}

func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition was not met before timeout")
}
