package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/tencent-connect/botgo"
	"github.com/tencent-connect/botgo/interaction/webhook"
	botgolog "github.com/tencent-connect/botgo/log"
	"github.com/tencent-connect/botgo/token"
)

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC | log.Lshortfile)
	cfg, err := LoadConfig()
	if err != nil {
		log.Fatal(err)
	}
	botgolog.DefaultLogger = newSafeBotGoLogger(cfg.Debug, cfg.AppSecret)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	credentials := &token.QQBotCredentials{AppID: cfg.AppID, AppSecret: cfg.AppSecret}
	tokenSource := token.NewQQBotTokenSource(credentials)
	if err := token.StartRefreshAccessToken(ctx, tokenSource); err != nil {
		log.Fatalf("获取 QQ Access Token 失败: %v", err)
	}
	api := botgo.NewOpenAPI(cfg.AppID, tokenSource).WithTimeout(cfg.OpenAPITimeout).SetDebug(cfg.Debug)
	processor := NewProcessor(api, cfg)
	intents := registerQQHandlers(processor)

	server := newHTTPServer(cfg, credentials)
	serverErrors := make(chan error, 1)
	go func() {
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErrors <- err
		}
	}()
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()

	if cfg.EventMode == EventModeWebhook {
		log.Printf("QQ Bot Webhook 已启动: %s%s (私聊 C2C)", cfg.ListenAddr, cfg.WebhookPath)
		select {
		case <-ctx.Done():
			return
		case err := <-serverErrors:
			log.Fatal(err)
		}
	}

	log.Printf("QQ Bot WebSocket 正在启动（健康检查: %s/healthz，私聊 C2C）", cfg.ListenAddr)
	gateway, err := api.WS(ctx, nil, "")
	if err != nil {
		log.Fatalf("获取 QQ WebSocket 网关失败: %v", err)
	}
	websocketErrors := make(chan error, 1)
	go func() {
		websocketErrors <- botgo.NewSessionManager().Start(gateway, tokenSource, &intents)
	}()

	select {
	case <-ctx.Done():
		return
	case err := <-serverErrors:
		log.Fatal(err)
	case err := <-websocketErrors:
		if err != nil {
			log.Fatalf("QQ WebSocket 会话退出: %v", err)
		}
		log.Fatal("QQ WebSocket 会话意外退出")
	}
}

func newHTTPServer(cfg Config, credentials *token.QQBotCredentials) *http.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"status":"ok","event_mode":%q}`, cfg.EventMode)
	})
	if cfg.EventMode == EventModeWebhook {
		mux.HandleFunc(cfg.WebhookPath, safeWebhookHandler(cfg, credentials))
	}

	return &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
}

func safeWebhookHandler(cfg Config, credentials *token.QQBotCredentials) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if recovered := recover(); recovered != nil {
				log.Printf("QQ Webhook panic: %v", recovered)
				http.Error(w, "internal error", http.StatusInternalServerError)
			}
		}()
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if r.ContentLength < 0 || r.ContentLength > cfg.MaxWebhookBytes {
			http.Error(w, fmt.Sprintf("invalid content length: %d", r.ContentLength), http.StatusRequestEntityTooLarge)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, cfg.MaxWebhookBytes)
		webhook.HTTPHandler(w, r, credentials)
	}
}
