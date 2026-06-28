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
	"github.com/tencent-connect/botgo/token"
)

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC | log.Lshortfile)
	cfg, err := LoadConfig()
	if err != nil {
		log.Fatal(err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	credentials := &token.QQBotCredentials{AppID: cfg.AppID, AppSecret: cfg.AppSecret}
	tokenSource := token.NewQQBotTokenSource(credentials)
	if err := token.StartRefreshAccessToken(ctx, tokenSource); err != nil {
		log.Fatalf("获取 QQ Access Token 失败: %v", err)
	}
	api := botgo.NewOpenAPI(cfg.AppID, tokenSource).WithTimeout(cfg.OpenAPITimeout).SetDebug(cfg.Debug)
	processor := NewProcessor(api, cfg)
	registerQQHandlers(processor, cfg)

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc(cfg.WebhookPath, safeWebhookHandler(cfg, credentials))

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()

	log.Printf("QQ Bot Webhook 已启动: %s%s (c2c=%t group=%t channel=%t)", cfg.ListenAddr, cfg.WebhookPath, cfg.EnableC2C, cfg.EnableGroup, cfg.EnableChannel)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
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
