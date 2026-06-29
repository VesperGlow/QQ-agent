package main

import (
	"encoding/json"
	"log"
	"strings"

	"github.com/tencent-connect/botgo/dto"
	botmessage "github.com/tencent-connect/botgo/dto/message"
	"github.com/tencent-connect/botgo/event"
)

type rawEnvelope struct {
	Data struct {
		ID          string `json:"id"`
		Content     string `json:"content"`
		ChannelID   string `json:"channel_id"`
		GuildID     string `json:"guild_id"`
		GroupID     string `json:"group_id"`
		GroupOpenID string `json:"group_openid"`
		Author      struct {
			ID           string `json:"id"`
			UserOpenID   string `json:"user_openid"`
			MemberOpenID string `json:"member_openid"`
			Bot          bool   `json:"bot"`
		} `json:"author"`
	} `json:"d"`
}

func registerQQHandlers(processor *Processor, cfg Config) dto.Intent {
	handlers := make([]interface{}, 0, 5)
	handlers = append(handlers,
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
	)
	if cfg.EnableC2C {
		handlers = append(handlers, c2cHandler(processor))
	}
	if cfg.EnableGroup {
		handlers = append(handlers, groupHandler(processor))
	}
	if cfg.EnableChannel {
		handlers = append(handlers, channelHandler(processor))
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

func groupHandler(processor *Processor) event.GroupATMessageEventHandler {
	return func(payload *dto.WSPayload, data *dto.WSGroupATMessageData) error {
		message := dto.Message(*data)
		raw := parseRaw(payload)
		messageID := firstNonEmpty(message.ID, raw.Data.ID)
		groupID := firstNonEmpty(message.GroupID, raw.Data.GroupOpenID, raw.Data.GroupID)
		senderID := raw.Data.Author.MemberOpenID
		if message.Author != nil {
			senderID = firstNonEmpty(message.Author.ID, senderID)
		}
		if raw.Data.Author.Bot || (message.Author != nil && message.Author.Bot) {
			return nil
		}
		content := firstNonEmpty(message.Content, raw.Data.Content)
		content = strings.TrimSpace(botmessage.ETLInput(content))
		userID, conversationID := stableIDs(ScopeGroup, groupID, senderID)
		processor.Submit(MessageJob{
			Kind: ScopeGroup, MessageID: messageID, ReplyTarget: groupID,
			UserID: userID, ConversationID: conversationID,
			Content: validUTF8(content), HasAttachments: attachmentPresent(message.Attachments),
		})
		return nil
	}
}

func channelHandler(processor *Processor) event.ATMessageEventHandler {
	return func(payload *dto.WSPayload, data *dto.WSATMessageData) error {
		message := dto.Message(*data)
		raw := parseRaw(payload)
		messageID := firstNonEmpty(message.ID, raw.Data.ID)
		channelID := firstNonEmpty(message.ChannelID, raw.Data.ChannelID)
		senderID := raw.Data.Author.ID
		if message.Author != nil {
			senderID = firstNonEmpty(message.Author.ID, senderID)
		}
		if raw.Data.Author.Bot || (message.Author != nil && message.Author.Bot) {
			return nil
		}
		content := firstNonEmpty(message.Content, raw.Data.Content)
		content = strings.TrimSpace(botmessage.ETLInput(content))
		userID, conversationID := stableIDs(ScopeChannel, channelID, senderID)
		processor.Submit(MessageJob{
			Kind: ScopeChannel, MessageID: messageID, ReplyTarget: channelID,
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
