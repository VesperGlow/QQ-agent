package main

import (
	"fmt"
	"log"
	"regexp"
	"strings"
)

var botGoSecretPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)("(?:clientSecret|appSecret|access_token|token)"\s*:\s*")[^"]*(")`),
	regexp.MustCompile(`(?i)(\b(?:AccessToken|ClientSecret|AppSecret):)\S+`),
}

type safeBotGoLogger struct {
	debug   bool
	secrets []string
}

func newSafeBotGoLogger(debug bool, secrets ...string) *safeBotGoLogger {
	return &safeBotGoLogger{debug: debug, secrets: secrets}
}

func (l *safeBotGoLogger) Debug(values ...interface{}) {
	if l.debug {
		l.output("DEBUG", fmt.Sprint(values...))
	}
}

func (l *safeBotGoLogger) Info(values ...interface{}) {
	if l.debug {
		l.output("INFO", fmt.Sprint(values...))
	}
}

func (l *safeBotGoLogger) Warn(values ...interface{}) {
	l.output("WARN", fmt.Sprint(values...))
}

func (l *safeBotGoLogger) Error(values ...interface{}) {
	l.output("ERROR", fmt.Sprint(values...))
}

func (l *safeBotGoLogger) Debugf(format string, values ...interface{}) {
	if l.debug {
		l.output("DEBUG", fmt.Sprintf(format, values...))
	}
}

func (l *safeBotGoLogger) Infof(format string, values ...interface{}) {
	if l.debug {
		l.output("INFO", fmt.Sprintf(format, values...))
	}
}

func (l *safeBotGoLogger) Warnf(format string, values ...interface{}) {
	l.output("WARN", fmt.Sprintf(format, values...))
}

func (l *safeBotGoLogger) Errorf(format string, values ...interface{}) {
	l.output("ERROR", fmt.Sprintf(format, values...))
}

func (l *safeBotGoLogger) Sync() error {
	return nil
}

func (l *safeBotGoLogger) output(level, message string) {
	for _, secret := range l.secrets {
		if secret != "" {
			message = strings.ReplaceAll(message, secret, "[REDACTED]")
		}
	}
	for _, pattern := range botGoSecretPatterns {
		message = pattern.ReplaceAllString(message, `${1}[REDACTED]${2}`)
	}
	log.Printf("BotGo %s: %s", level, message)
}
