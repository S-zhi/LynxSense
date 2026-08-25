package oauth

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

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
