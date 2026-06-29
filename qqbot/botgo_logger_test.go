package main

import (
	"bytes"
	"log"
	"strings"
	"testing"
)

func TestSafeBotGoLoggerRedactsCredentials(t *testing.T) {
	var output bytes.Buffer
	originalWriter := log.Writer()
	log.SetOutput(&output)
	t.Cleanup(func() { log.SetOutput(originalWriter) })

	logger := newSafeBotGoLogger(true, "app-secret-value")
	logger.Infof(`identify {"token":"QQBot access-token-value","clientSecret":"app-secret-value"}`)

	logged := output.String()
	if strings.Contains(logged, "access-token-value") || strings.Contains(logged, "app-secret-value") {
		t.Fatalf("logger leaked credentials: %s", logged)
	}
	if strings.Count(logged, "[REDACTED]") < 2 {
		t.Fatalf("logger did not redact expected fields: %s", logged)
	}
}

func TestSafeBotGoLoggerSuppressesInfoByDefault(t *testing.T) {
	var output bytes.Buffer
	originalWriter := log.Writer()
	log.SetOutput(&output)
	t.Cleanup(func() { log.SetOutput(originalWriter) })

	logger := newSafeBotGoLogger(false)
	logger.Info("raw websocket frame")
	logger.Error("connection failed")

	logged := output.String()
	if strings.Contains(logged, "raw websocket frame") || !strings.Contains(logged, "connection failed") {
		t.Fatalf("unexpected log output: %s", logged)
	}
}
