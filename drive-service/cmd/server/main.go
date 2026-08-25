// Command server starts the local Google Drive sidecar used by the Python web
// application. All service dependencies are singletons for this process.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	driveclient "github.com/s-zhi/subtitles-ai-drive/internal/drive"
	"github.com/s-zhi/subtitles-ai-drive/internal/httpapi"
	"github.com/s-zhi/subtitles-ai-drive/internal/oauth"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
	"github.com/s-zhi/subtitles-ai-drive/internal/transfer"
)

func main() {
	configPath := flag.String("config", "./config.local.json", "path to local JSON config")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Printf("配置文件未加载: %v；使用默认配置", err)
		cfg = config.Default()
	}
	if err := cfg.EnsureDataDir(); err != nil {
		log.Fatal(err)
	}
	if err := os.MkdirAll(cfg.StagingDir(), 0o700); err != nil {
		log.Fatal(err)
	}
	state, err := store.Open(cfg.StatePath())
	if err != nil {
		log.Fatal(err)
	}
	auth := oauth.NewManager(&cfg)
	drive := driveclient.NewClient(&cfg, auth, state)
	transfers := transfer.NewManager(&cfg, state, drive)
	// Re-queue durable non-terminal transfers before accepting new requests.
	transfers.Recover()

	// ReadHeaderTimeout only bounds request-header parsing. Body streaming is
	// governed by request context and the per-client transfer pipeline, while
	// IdleTimeout applies between keep-alive requests rather than upload bytes.
	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           httpapi.New(&cfg, auth, drive, transfers, state),
		ReadHeaderTimeout: 15 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	log.Printf("Subtitles AI Drive service listening on http://%s", cfg.ListenAddr)
	log.Printf("Google OAuth credentials are intentionally empty until configured")
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
