// Package oauth 实现单用户 Google OAuth 流程，并将 Refresh Token 持久化到 sidecar
// 数据目录中。用户 Token 不会写入受源码管理的配置。
package oauth

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"sync"
	"time"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	"golang.org/x/oauth2"
)

type clientDetails struct {
	ClientID     string   `json:"client_id"`
	ClientSecret string   `json:"client_secret"`
	AuthURI      string   `json:"auth_uri"`
	TokenURI     string   `json:"token_uri"`
	RedirectURIs []string `json:"redirect_uris"`
}

type clientFile struct {
	Installed *clientDetails `json:"installed"`
	Web       *clientDetails `json:"web"`
}

type pendingAuth struct {
	cfg      *oauth2.Config
	state    string
	verifier string
	server   *http.Server
	listener net.Listener
}

// Manager 管理一次浏览器授权流程，以及进程级 Drive 客户端后续的 Token 刷新。
type Manager struct {
	cfg *config.Config

	mu      sync.Mutex
	pending *pendingAuth

	clientID     string
	clientSecret string
	clientLoaded bool
}

// NewManager 创建 OAuth 管理器，不执行网络 I/O。
func NewManager(cfg *config.Config) *Manager { return &Manager{cfg: cfg} }

// credentials 按“配置字段优先、客户端 JSON 兜底”的顺序加载 OAuth 应用凭据。
func (m *Manager) credentials() (string, string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.clientLoaded && m.clientID != "" && m.clientSecret != "" {
		return m.clientID, m.clientSecret, nil
	}
	m.clientLoaded = true
	m.clientID = m.cfg.GoogleClientID
	m.clientSecret = m.cfg.GoogleClientSecret
	// Missing credentials are not cached as a permanent failure: the operator
	// can add oauth_client.json while the sidecar remains running.
	if m.clientID == "" || m.clientSecret == "" {
		path := m.cfg.ClientConfigPath()
		b, err := os.ReadFile(path)
		if err == nil {
			var file clientFile
			if err := json.Unmarshal(b, &file); err != nil {
				return "", "", fmt.Errorf("解析 Google OAuth 配置失败: %w", err)
			}
			d := file.Installed
			if d == nil {
				d = file.Web
			}
			if d != nil {
				if m.clientID == "" {
					m.clientID = d.ClientID
				}
				if m.clientSecret == "" {
					m.clientSecret = d.ClientSecret
				}
			}
		} else if !errors.Is(err, os.ErrNotExist) {
			return "", "", fmt.Errorf("读取 Google OAuth 配置失败: %w", err)
		}
	}
	if m.clientID == "" || m.clientSecret == "" {
		// 不缓存缺失配置；sidecar 运行期间用户仍可能补充 OAuth 客户端 JSON。
		m.clientLoaded = false
		return "", "", errors.New("Google OAuth ClientID/ClientSecret 尚未配置；请填写配置或导入 Desktop OAuth JSON")
	}
	return m.clientID, m.clientSecret, nil
}

// oauthConfig 为 loopback 回调或显式配置的回调地址构造 Google OAuth 配置。
func (m *Manager) oauthConfig(redirect string) (*oauth2.Config, error) {
	id, secret, err := m.credentials()
	if err != nil {
		return nil, err
	}
	return &oauth2.Config{
		ClientID:     id,
		ClientSecret: secret,
		Endpoint: oauth2.Endpoint{
			AuthURL:  "https://accounts.google.com/o/oauth2/v2/auth",
			TokenURL: "https://oauth2.googleapis.com/token",
		},
		RedirectURL: redirect,
		Scopes:      m.cfg.GoogleScopes,
	}, nil
}

// Start 启动浏览器授权流程，并始终创建随机端口的临时 loopback 监听器。
// 这是本地 Desktop OAuth 客户端推荐的动态回调方式。
func (m *Manager) Start() (string, error) {
	// 绑定端口 0 让操作系统自动选择未占用的 loopback 端口，避免固定端口，
	// 也降低与其他本地服务冲突的可能性。
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", fmt.Errorf("start OAuth loopback listener: %w", err)
	}
	redirect := "http://" + listener.Addr().String()
	ocfg, err := m.oauthConfig(redirect)
	if err != nil {
		_ = listener.Close()
		return "", err
	}
	state, err := randomString(32)
	if err != nil {
		return "", err
	}
	verifier, err := randomString(48)
	if err != nil {
		return "", err
	}
	// state 用于防止登录响应串线；verifier 启用 PKCE，即使授权码在到达回调前
	// 被截获，也不能直接兑换 Token。
	p := &pendingAuth{cfg: ocfg, state: state, verifier: verifier, listener: listener}
	m.mu.Lock()
	if m.pending != nil && m.pending.listener != nil {
		_ = m.pending.listener.Close()
	}
	m.pending = p
	m.mu.Unlock()

	p.server = &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		m.finishCallback(w, r, p)
	})}
	go func() {
		_ = p.server.Serve(listener)
	}()
	return ocfg.AuthCodeURL(state, oauth2.AccessTypeOffline, oauth2.S256ChallengeOption(verifier)), nil
}

// finishCallback 校验 OAuth state 和 PKCE 参数，兑换授权码并保存 Refresh Token。
func (m *Manager) finishCallback(w http.ResponseWriter, r *http.Request, p *pendingAuth) {
	if r.URL.Query().Get("state") != p.state {
		http.Error(w, "OAuth state 校验失败", http.StatusBadRequest)
		return
	}
	if oauthErr := r.URL.Query().Get("error"); oauthErr != "" {
		http.Error(w, "Google OAuth 被拒绝: "+html.EscapeString(oauthErr), http.StatusBadRequest)
		m.clearPending(p)
		return
	}
	code := r.URL.Query().Get("code")
	if code == "" {
		http.Error(w, "Google OAuth 未返回授权 code", http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	tok, err := p.cfg.Exchange(ctx, code, oauth2.VerifierOption(p.verifier))
	if err != nil {
		http.Error(w, "交换 Google OAuth Token 失败: "+html.EscapeString(err.Error()), http.StatusBadGateway)
		m.clearPending(p)
		return
	}
	if err := m.saveToken(tok); err != nil {
		http.Error(w, "保存 Google OAuth Token 失败: "+html.EscapeString(err.Error()), http.StatusInternalServerError)
		m.clearPending(p)
		return
	}
	m.clearPending(p)
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = io.WriteString(w, "<html><body><h3>Google Drive 已连接</h3><p>可以关闭此页面返回应用。</p></body></html>")
}

// clearPending 清理当前授权流程，并关闭其临时 HTTP 服务或 loopback 监听器。
func (m *Manager) clearPending(p *pendingAuth) {
	m.mu.Lock()
	if m.pending == p {
		m.pending = nil
	}
	m.mu.Unlock()
	if p.server != nil {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		_ = p.server.Shutdown(ctx)
		cancel()
	} else if p.listener != nil {
		_ = p.listener.Close()
	}
}

// saveToken 以受限权限原子写入 OAuth Token，避免留下半写入文件。
func (m *Manager) saveToken(tok *oauth2.Token) error {
	b, err := json.MarshalIndent(tok, "", "  ")
	if err != nil {
		return err
	}
	tmp := m.cfg.TokenPath() + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, m.cfg.TokenPath())
}

// loadToken 从本地数据目录读取 OAuth Token；文件不存在表示尚未授权。
func (m *Manager) loadToken() (*oauth2.Token, error) {
	b, err := os.ReadFile(m.cfg.TokenPath())
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var tok oauth2.Token
	if err := json.Unmarshal(b, &tok); err != nil {
		return nil, fmt.Errorf("parse OAuth token: %w", err)
	}
	return &tok, nil
}

// TokenSource 返回由持久化 Token 支持的可刷新 TokenSource。刷新后的 Token 会
// 写回磁盘，使后续上传和下载无需感知 Access Token 过期。
func (m *Manager) TokenSource(ctx context.Context) (oauth2.TokenSource, error) {
	tok, err := m.loadToken()
	if err != nil {
		return nil, err
	}
	if tok == nil || tok.RefreshToken == "" {
		return nil, errors.New("尚未完成 Google Drive 授权")
	}
	// 刷新 Token 不会再次发送 redirect_uri；这里仅提供一个占位值构造
	// oauth2.Config，真正的动态回调地址只在首次授权码兑换时使用。
	ocfg, err := m.oauthConfig("http://127.0.0.1")
	if err != nil {
		return nil, err
	}
	inner := ocfg.TokenSource(ctx, tok)
	return &persistingSource{inner: oauth2.ReuseTokenSource(tok, inner), save: m.saveToken}, nil
}

// HTTPClient 返回带认证信息且使用配置请求超时的 HTTP 客户端。请求取消仍优先于
// 此超时生效。
func (m *Manager) HTTPClient(ctx context.Context) (*http.Client, error) {
	source, err := m.TokenSource(ctx)
	if err != nil {
		return nil, err
	}
	client := oauth2.NewClient(ctx, source)
	if m.cfg.RequestTimeoutSeconds > 0 {
		client.Timeout = time.Duration(m.cfg.RequestTimeoutSeconds) * time.Second
	}
	return client, nil
}

// Disconnect 尽可能撤销当前 Access Token，并删除本地 Refresh Token；下次使用时
// 需要重新通过浏览器登录。
func (m *Manager) Disconnect(ctx context.Context) error {
	tok, err := m.loadToken()
	if err != nil {
		return err
	}
	if tok != nil && tok.AccessToken != "" {
		client := &http.Client{Timeout: 15 * time.Second}
		form := url.Values{"token": {tok.AccessToken}}
		req, reqErr := http.NewRequestWithContext(ctx, http.MethodPost, "https://oauth2.googleapis.com/revoke", stringsReader(form.Encode()))
		if reqErr == nil {
			req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
			resp, doErr := client.Do(req)
			if doErr == nil {
				_ = resp.Body.Close()
			}
		}
	}
	if err := os.Remove(m.cfg.TokenPath()); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// Status 返回配置和连接状态，但不会暴露任何 Token。
func (m *Manager) Status() map[string]any {
	id, _, credErr := m.credentials()
	tok, tokErr := m.loadToken()
	connected := tokErr == nil && tok != nil && tok.RefreshToken != ""
	reauthorize := credErr == nil && (tokErr != nil || tok == nil || tok.RefreshToken == "")
	return map[string]any{
		"configured": credErr == nil && id != "",
		"connected":  connected,
		"token_expiry": func() any {
			if tok == nil {
				return nil
			}
			return tok.Expiry
		}(),
		"reauthorize": reauthorize,
		"token_error": func() string {
			if tokErr != nil {
				return tokErr.Error()
			}
			return ""
		}(),
		"configuration_error": func() string {
			if credErr != nil {
				return credErr.Error()
			}
			return ""
		}(),
	}
}

// persistingSource 在允许 oauth2 按需刷新 Access Token 的同时，去重 Token 写入。
type persistingSource struct {
	inner oauth2.TokenSource
	save  func(*oauth2.Token) error
	mu    sync.Mutex
	last  string
}

// Token 获取当前有效 Token，并仅在 Token 内容变化时持久化刷新结果。
func (s *persistingSource) Token() (*oauth2.Token, error) {
	tok, err := s.inner.Token()
	if err != nil {
		return nil, err
	}
	key := tok.AccessToken + "|" + tok.RefreshToken + "|" + tok.Expiry.String()
	s.mu.Lock()
	defer s.mu.Unlock()
	if key != s.last {
		if err := s.save(tok); err != nil {
			return nil, err
		}
		s.last = key
	}
	return tok, nil
}

// randomString 使用密码学安全随机数生成 state 或 PKCE verifier。
func randomString(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// stringsReader 将字符串包装成 io.Reader，用于构造撤销 Token 的请求体。
func stringsReader(value string) io.Reader { return &stringReader{value: value} }

type stringReader struct {
	value  string
	offset int
}

// Read 按顺序读取字符串内容，直到所有字节都被消费完毕。
func (r *stringReader) Read(p []byte) (int, error) {
	if r.offset >= len(r.value) {
		return 0, io.EOF
	}
	n := copy(p, r.value[r.offset:])
	r.offset += n
	return n, nil
}
