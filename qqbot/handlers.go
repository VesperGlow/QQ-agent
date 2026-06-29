package main

import (
	"encoding/json"
	"log"
	"strings"

	"github.com/tencent-connect/botgo/dto"
	"github.com/tencent-connect/botgo/event"
)

type rawEnvelope struct {
	Data struct {
		ID      string `json:"id"`
		Content string `json:"content"`
		Author  struct {
			ID         string `json:"id"`
			UserOpenID string `json:"user_openid"`
			Bot        bool   `json:"bot"`
		} `json:"author"`
	} `json:"d"`
}

func registerQQHandlers(processor *Processor) dto.Intent {
	handlers := []interface{}{
		event.ReadyHandler(func(_ *dto.WSPayload, data *dto.WSReadyData) {
			if data == nil {
				log.Print("QQ WebSocket 已连接")
				return
			}
			log.Printf("QQ WebSocket 已连接 (shard=%v)", data.Shard)
		}),
		event.ErrorNotifyHandler(func(err error) {
			log.Printf("QQ WebSocket 连接异常，BotGo 将尝试恢复: %v", err)
		}),
		c2cHandler(processor),
	}
	return event.RegisterHandlers(handlers...)
}

func c2cHandler(processor *Processor) event.C2CMessageEventHandler {
	return func(payload *dto.WSPayload, data *dto.WSC2CMessageData) error {
		message := dto.Message(*data)
		raw := parseRaw(payload)
		messageID := firstNonEmpty(message.ID, raw.Data.ID)
		content := firstNonEmpty(message.Content, raw.Data.Content)
		senderID := raw.Data.Author.UserOpenID
		if message.Author != nil {
			senderID = firstNonEmpty(message.Author.ID, senderID)
		}
		if raw.Data.Author.Bot || (message.Author != nil && message.Author.Bot) {
			return nil
		}
		userID, conversationID := stableIDs(ScopeC2C, senderID, senderID)
		processor.Submit(MessageJob{
			Kind: ScopeC2C, MessageID: messageID, ReplyTarget: senderID,
			UserID: userID, ConversationID: conversationID,
			Content: validUTF8(content), HasAttachments: attachmentPresent(message.Attachments),
		})
		return nil
	}
}

func parseRaw(payload *dto.WSPayload) rawEnvelope {
	var result rawEnvelope
	if payload == nil || len(payload.RawMessage) == 0 {
		return result
	}
	if err := json.Unmarshal(payload.RawMessage, &result); err != nil {
		log.Printf("解析 QQ 原始事件失败: %v", err)
	}
	return result
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}
