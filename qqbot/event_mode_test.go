package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tencent-connect/botgo/token"
)

func setRequiredConfigEnv(t *testing.T) {
	t.Helper()
	t.Setenv("QQ_APP_ID", "test-app")
	t.Setenv("QQ_APP_SECRET", "test-secret")
	t.Setenv("QQ_AI_URL", "http://app:8000/v1/chat")
}

func TestLoadConfigEventMode(t *testing.T) {
	setRequiredConfigEnv(t)
	t.Setenv("QQ_EVENT_MODE", "")

	cfg, err := LoadConfig()
	if err != nil {
		t.Fatalf("LoadConfig() error = %v", err)
	}
	if cfg.EventMode != EventModeWebhook {
		t.Fatalf("default EventMode = %q, want %q", cfg.EventMode, EventModeWebhook)
	}

	t.Setenv("QQ_EVENT_MODE", " WebSocket ")
	cfg, err = LoadConfig()
	if err != nil {
		t.Fatalf("LoadConfig() websocket error = %v", err)
	}
	if cfg.EventMode != EventModeWebSocket {
		t.Fatalf("EventMode = %q, want %q", cfg.EventMode, EventModeWebSocket)
	}
}

func TestLoadConfigRejectsInvalidEventMode(t *testing.T) {
	setRequiredConfigEnv(t)
	t.Setenv("QQ_EVENT_MODE", "polling")

	_, err := LoadConfig()
	if err == nil || !strings.Contains(err.Error(), "QQ_EVENT_MODE") {
		t.Fatalf("LoadConfig() error = %v, want QQ_EVENT_MODE validation error", err)
	}
}

func TestHTTPRoutesFollowEventMode(t *testing.T) {
	credentials := &token.QQBotCredentials{AppID: "test-app", AppSecret: "test-secret"}

	for _, test := range []struct {
		name           string
		mode           string
		webhookStatus  int
		healthContains string
	}{
		{name: "webhook", mode: EventModeWebhook, webhookStatus: http.StatusMethodNotAllowed, healthContains: `"event_mode":"webhook"`},
		{name: "websocket", mode: EventModeWebSocket, webhookStatus: http.StatusNotFound, healthContains: `"event_mode":"websocket"`},
	} {
		t.Run(test.name, func(t *testing.T) {
			cfg := Config{EventMode: test.mode, WebhookPath: "/qqbot", MaxWebhookBytes: 1024}
			server := newHTTPServer(cfg, credentials)

			webhookResponse := httptest.NewRecorder()
			server.Handler.ServeHTTP(webhookResponse, httptest.NewRequest(http.MethodGet, "/qqbot", nil))
			if webhookResponse.Code != test.webhookStatus {
				t.Fatalf("GET /qqbot status = %d, want %d", webhookResponse.Code, test.webhookStatus)
			}

			healthResponse := httptest.NewRecorder()
			server.Handler.ServeHTTP(healthResponse, httptest.NewRequest(http.MethodGet, "/healthz", nil))
			if healthResponse.Code != http.StatusOK || !strings.Contains(healthResponse.Body.String(), test.healthContains) {
				t.Fatalf("GET /healthz = %d %q", healthResponse.Code, healthResponse.Body.String())
			}
		})
	}
}
