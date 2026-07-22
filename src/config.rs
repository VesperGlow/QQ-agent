//! 环境变量配置：变量名保持稳定，现有 .env 无需改动。

use std::env;

use anyhow::{bail, Context, Result};
use serde::Serialize;
use serde_json::{Map, Value};

use crate::llm::Think;

fn env_string(name: &str, fallback: &str) -> String {
    match env::var(name) {
        Ok(value) if !value.trim().is_empty() => value.trim().to_string(),
        _ => fallback.to_string(),
    }
}

fn env_parse<T: std::str::FromStr>(name: &str, fallback: T) -> T {
    env::var(name)
        .ok()
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(fallback)
}

fn env_bool(name: &str, fallback: bool) -> bool {
    match env::var(name) {
        Ok(value) => matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        Err(_) => fallback,
    }
}

fn clamp<T: PartialOrd>(value: T, min: T, max: T) -> T {
    if value < min {
        min
    } else if value > max {
        max
    } else {
        value
    }
}

/// 在字符串里展开 ${NAME} / $NAME 环境变量引用（MCP 配置用）。
pub fn expand_env_refs(value: &str) -> Result<String> {
    let mut result = String::with_capacity(value.len());
    let bytes = value.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'$' && i + 1 < bytes.len() {
            let (name, consumed) = if bytes[i + 1] == b'{' {
                let end = value[i + 2..]
                    .find('}')
                    .with_context(|| format!("MCP 配置里有未闭合的 ${{：{value}"))?;
                (&value[i + 2..i + 2 + end], end + 3)
            } else {
                let rest = &value[i + 1..];
                let end = rest
                    .find(|c: char| !c.is_ascii_alphanumeric() && c != '_')
                    .unwrap_or(rest.len());
                (&rest[..end], end + 1)
            };
            if name.is_empty() {
                result.push('$');
                i += 1;
                continue;
            }
            let resolved = env::var(name)
                .with_context(|| format!("MCP_SERVERS_JSON 引用了未设置的环境变量 {name}"))?;
            result.push_str(&resolved);
            i += consumed;
        } else {
            result.push(bytes[i] as char);
            i += 1;
        }
    }
    Ok(result)
}

#[derive(Debug, Clone)]
pub struct McpServer {
    pub name: String,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub include: Vec<String>,
    pub exclude: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QqEventMode {
    Webhook,
    WebSocket,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub app_api_key: String,
    pub log_level: String,
    /// 运行日志里消息/记忆内容预览的最大字符数；0 = 完全不在日志里出现内容。
    pub log_preview_chars: usize,
    pub persona_prompt: String,
    pub system_instructions: String,

    pub ai_base_url: String,
    pub ai_api_key: String,
    pub memory_model: String,
    pub chat_model: String,
    /// 记忆模型的独立接入点；为空时回退到 ai_* 共享配置。
    /// 这样对话模型可用 grok、廉价的记忆模型可用 deepseek 等不同供应商。
    pub memory_base_url: String,
    pub memory_api_key: String,
    pub memory_extra_headers: Vec<(String, String)>,
    pub ai_timeout_seconds: f64,
    pub ai_max_retries: u32,
    pub ai_extra_headers: Vec<(String, String)>,
    /// 对话调用的思考深度；配 ai_thinking_map 翻译成具体厂商字段。
    pub chat_think: Think,
    /// 「思考等级 → 要合并进请求体的 JSON 片段」映射，对话/记忆两个接入点各一份。
    /// 记忆侧调用（判定、摘要）恒为 off，一般无需配 memory 映射。
    pub ai_thinking_map: Map<String, Value>,
    pub memory_thinking_map: Map<String, Value>,
    pub chat_max_output_tokens: u32,
    pub memory_max_output_tokens: u32,
    pub max_tool_rounds: u32,

    pub mcp_servers: Vec<McpServer>,
    pub mcp_timeout_seconds: f64,
    pub mcp_result_max_chars: usize,

    /// 内置网页抓取工具（fetch_url）：拉取公开链接抽正文转 Markdown，不渲染 JS。
    pub fetch_url_enabled: bool,
    pub fetch_timeout_seconds: f64,
    pub fetch_max_bytes: usize,
    pub fetch_result_max_chars: usize,

    pub db_path: String,

    /// 检索最终注入上下文的记忆条数上限（精选后截断到这个数）。
    pub memory_search_limit: usize,
    pub memory_history_messages: i64,
    /// 近似去重阈值：新记忆与同 user+subject 的已有记忆的字符三元组 Jaccard 相似度
    /// ≥ 此值即合并。只兜「改了个标点/语气词」这类字面近重，语义重复由记忆模型 reconcile。
    pub memory_duplicate_threshold: f32,

    /// 记忆精选（检索）：候选池上限。按 last_seen_at 倒序取这么多条活跃记忆交给记忆模型挑。
    /// 调大提升召回上限（更老的记忆也进得来），代价是每次精选的输入 token 线性增长。
    pub memory_select_pool_max: usize,
    /// 候选清单里每条记忆的截断长度（字符）。挑选只需看个大意，超长记忆没必要整条喂。
    pub memory_select_text_max_chars: usize,
    /// 精选时查询文本的截断长度（字符）。
    pub memory_select_query_max_chars: usize,
    /// 精选调用的输出上限。只输出一个编号数组，几百 token 绰绰有余。
    pub memory_select_max_output_tokens: u32,
    /// 自动记忆巩固：短期窗口滑出的旧消息批量交给记忆模型抽取/reconcile 成长期记忆。
    /// 取代了旧的「每轮筛选用户单句」，改到压缩时对整批做，上下文更完整。
    pub memory_consolidate_enabled: bool,
    /// 触发一次巩固所需的「已滑出窗口、尚未巩固」的最少消息数。
    pub memory_consolidate_batch: usize,
    /// 尾巴 flush：定时扫描「空闲够久、且还有未巩固消息」的会话，强制把最后一段
    /// （含仍在短期窗口内、平时不会 evict 的部分）也巩固掉，避免用户长期沉默时尾巴丢失。
    pub memory_flush_enabled: bool,
    /// 会话最后活动早于「现在 - 该秒数」才算空闲、可被 flush。
    pub memory_flush_idle_seconds: u64,
    /// flush 扫描周期（秒）。
    pub memory_flush_interval_seconds: u64,

    pub conversation_summary_enabled: bool,
    pub conversation_summary_batch: usize,
    pub conversation_summary_max_chars: usize,

    /// 图片理解：把 QQ 图片附件 / API images 参数以 image_url 段传给对话模型（模型须支持视觉）。
    pub chat_image_enabled: bool,
    pub chat_image_max_count: usize,
    /// 发送给模型的单张图片大小上限；超限的图会先压缩（缩放 + 重编码 JPEG）。
    pub chat_image_max_bytes: usize,
    /// 从 QQ CDN 下载原图的大小上限（压缩前），防滥用兜底。
    pub chat_image_fetch_max_bytes: usize,
    /// **解码前**的像素数闸门，超过就直接拒绝。这是容器内存峰值的实际决定因素：
    /// `chat_image_max_edge` 管的是输出尺寸，缩放却在解码之后发生，所以不设这道闸，
    /// 一张超大原图会先解成几百 MB 的缓冲。按最坏路径约 12 字节/像素估内存。
    pub chat_image_max_pixels: u32,
    /// 图片长边像素上限，超过则缩放；视觉模型内部分辨率有限，缩了还省 token。
    pub chat_image_max_edge: u32,

    pub mood_tracking_enabled: bool,
    pub mood_trend_days: i64,
    pub time_awareness_enabled: bool,

    pub app_port: u16,

    /// 收到 SIGTERM/Ctrl-C 后等待在途消息与落库任务完成的上限（秒）。
    /// 注意容器编排的 stop 宽限期（如 podman stop -t / stop_grace_period）要不小于该值。
    pub shutdown_timeout_seconds: u64,

    // QQ 桥接
    pub qq_app_id: String,
    pub qq_app_secret: String,
    pub qq_event_mode: QqEventMode,
    pub qq_listen_addr: String,
    pub qq_webhook_path: String,
    pub qq_ai_timeout_seconds: u64,
    pub qq_openapi_timeout_seconds: u64,
    pub qq_reply_max_runes: usize,
    pub qq_reply_max_parts: usize,
    pub qq_workers: usize,
    pub qq_queue_size: usize,
    pub qq_dedup_ttl_seconds: u64,
    pub qq_max_webhook_bytes: usize,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        let mcp_servers = parse_mcp_servers(&env_string("MCP_SERVERS_JSON", "[]"))?;
        let ai_extra_headers = parse_headers(&env_string("AI_EXTRA_HEADERS_JSON", "{}"))
            .context("AI_EXTRA_HEADERS_JSON 必须是 JSON 对象")?;

        // 记忆模型接入点：未单独设置时回退到共享的 ai_* 配置。
        let memory_base_url = {
            let raw = env_string("MEMORY_BASE_URL", "").trim_end_matches('/').to_string();
            if raw.is_empty() {
                env_string("AI_BASE_URL", "").trim_end_matches('/').to_string()
            } else {
                raw
            }
        };
        let memory_api_key = {
            let raw = env_string("MEMORY_API_KEY", "");
            if raw.is_empty() {
                env_string("AI_API_KEY", "")
            } else {
                raw
            }
        };
        let memory_extra_headers = match env::var("MEMORY_EXTRA_HEADERS_JSON") {
            Ok(value) if !value.trim().is_empty() => parse_headers(&value)
                .context("MEMORY_EXTRA_HEADERS_JSON 必须是 JSON 对象")?,
            _ => ai_extra_headers.clone(),
        };

        let chat_think = {
            let raw = env_string("CHAT_THINK", "high");
            Think::parse(&raw)
                .with_context(|| format!("CHAT_THINK 只能是 off/low/medium/high，当前是 {raw}"))?
        };
        let ai_thinking_map = parse_thinking_map(&env_string("AI_THINKING_MAP_JSON", "{}"))
            .context("AI_THINKING_MAP_JSON 必须是 {等级: {字段片段}} 形式的 JSON")?;
        // 未单独设置记忆映射时，与接入点回退逻辑一致：沿用对话侧映射。
        let memory_thinking_map = match env::var("MEMORY_THINKING_MAP_JSON") {
            Ok(value) if !value.trim().is_empty() => parse_thinking_map(&value)
                .context("MEMORY_THINKING_MAP_JSON 必须是 {等级: {字段片段}} 形式的 JSON")?,
            _ => ai_thinking_map.clone(),
        };

        let qq_event_mode = match env_string("QQ_EVENT_MODE", "webhook").to_lowercase().as_str() {
            "webhook" => QqEventMode::Webhook,
            "websocket" => QqEventMode::WebSocket,
            other => bail!("QQ_EVENT_MODE 必须是 webhook 或 websocket，当前是 {other}"),
        };

        let mut qq_webhook_path = env_string("QQ_WEBHOOK_PATH", "/qqbot");
        if !qq_webhook_path.starts_with('/') {
            qq_webhook_path.insert(0, '/');
        }

        Ok(Self {
            app_api_key: env_string("APP_API_KEY", ""),
            log_level: env_string("LOG_LEVEL", "INFO"),
            log_preview_chars: clamp(env_parse("LOG_CONTENT_PREVIEW_CHARS", 40), 0, 500),
            persona_prompt: env_string("PERSONA_PROMPT", ""),
            system_instructions: env_string("SYSTEM_INSTRUCTIONS", ""),

            ai_base_url: env_string("AI_BASE_URL", "").trim_end_matches('/').to_string(),
            ai_api_key: env_string("AI_API_KEY", ""),
            memory_model: env_string("MEMORY_MODEL", ""),
            chat_model: env_string("CHAT_MODEL", ""),
            memory_base_url,
            memory_api_key,
            memory_extra_headers,
            ai_timeout_seconds: env_parse("AI_TIMEOUT_SECONDS", 120.0),
            ai_max_retries: env_parse("AI_MAX_RETRIES", 2),
            ai_extra_headers,
            chat_think,
            ai_thinking_map,
            memory_thinking_map,
            chat_max_output_tokens: env_parse("CHAT_MAX_OUTPUT_TOKENS", 2048),
            memory_max_output_tokens: env_parse("MEMORY_MAX_OUTPUT_TOKENS", 800),
            max_tool_rounds: env_parse("MAX_TOOL_ROUNDS", 6),

            mcp_servers,
            mcp_timeout_seconds: env_parse("MCP_TIMEOUT_SECONDS", 300.0),
            mcp_result_max_chars: env_parse("MCP_RESULT_MAX_CHARS", 12000),

            fetch_url_enabled: env_bool("FETCH_URL_ENABLED", true),
            fetch_timeout_seconds: env_parse("FETCH_TIMEOUT_SECONDS", 30.0),
            fetch_max_bytes: clamp(env_parse("FETCH_MAX_BYTES", 5_242_880), 65_536, 52_428_800),
            fetch_result_max_chars: clamp(env_parse("FETCH_RESULT_MAX_CHARS", 12000), 500, 60000),

            db_path: env_string("DB_PATH", "/data/memory.db"),

            memory_search_limit: clamp(env_parse("MEMORY_SEARCH_LIMIT", 8), 1, 50),
            memory_history_messages: clamp(env_parse("MEMORY_HISTORY_MESSAGES", 16), 0, 100),
            // 0.9 ≈「只差标点或一两个语气词」。标点在比较前已被滤掉，所以纯加句号的改写是
            // 1.0；而「喜欢 X」与「不喜欢 X」只有 0.3 左右，不会被误合并。
            memory_duplicate_threshold: clamp(
                env_parse("MEMORY_DUPLICATE_THRESHOLD", 0.9),
                0.5,
                1.0,
            ),
            memory_select_pool_max: clamp(env_parse("MEMORY_SELECT_POOL_MAX", 400), 10, 5000),
            memory_select_text_max_chars: clamp(
                env_parse("MEMORY_SELECT_TEXT_MAX_CHARS", 200),
                20,
                2000,
            ),
            memory_select_query_max_chars: clamp(
                env_parse("MEMORY_SELECT_QUERY_MAX_CHARS", 2000),
                100,
                20000,
            ),
            memory_select_max_output_tokens: clamp(
                env_parse("MEMORY_SELECT_MAX_OUTPUT_TOKENS", 300),
                50,
                4000,
            ),
            memory_consolidate_enabled: env_bool("MEMORY_CONSOLIDATE_ENABLED", true),
            memory_consolidate_batch: clamp(env_parse("MEMORY_CONSOLIDATE_BATCH", 6), 2, 100),
            memory_flush_enabled: env_bool("MEMORY_FLUSH_ENABLED", true),
            memory_flush_idle_seconds: clamp(env_parse("MEMORY_FLUSH_IDLE_SECONDS", 900), 60, 86400),
            memory_flush_interval_seconds: clamp(
                env_parse("MEMORY_FLUSH_INTERVAL_SECONDS", 300),
                30,
                3600,
            ),

            conversation_summary_enabled: env_bool("CONVERSATION_SUMMARY_ENABLED", true),
            conversation_summary_batch: clamp(env_parse("CONVERSATION_SUMMARY_BATCH", 10), 2, 100),
            conversation_summary_max_chars: clamp(
                env_parse("CONVERSATION_SUMMARY_MAX_CHARS", 1000),
                100,
                8000,
            ),

            chat_image_enabled: env_bool("CHAT_IMAGE_ENABLED", true),
            chat_image_max_count: clamp(env_parse("CHAT_IMAGE_MAX_COUNT", 3), 1, 10),
            chat_image_max_bytes: clamp(env_parse("CHAT_IMAGE_MAX_BYTES", 5_242_880), 65_536, 20_971_520),
            chat_image_fetch_max_bytes: clamp(
                env_parse("CHAT_IMAGE_FETCH_MAX_BYTES", 31_457_280),
                1_048_576,
                104_857_600,
            ),
            chat_image_max_edge: clamp(env_parse("CHAT_IMAGE_MAX_EDGE", 2048), 512, 8192),
            // 16MP 覆盖 4K 截图与主流手机照片（12MP），按 12 字节/像素约 192MB 解码峰值，
            // 落在默认 mem_limit=512m 内；把 mem_limit 压到 256m 时要一并调到 8MP 左右。
            // 上界 200MP 只防配置写错，真按它配需要自行放宽 mem_limit。
            chat_image_max_pixels: clamp(
                env_parse("CHAT_IMAGE_MAX_PIXELS", 16_000_000),
                1_000_000,
                200_000_000,
            ),

            mood_tracking_enabled: env_bool("MOOD_TRACKING_ENABLED", true),
            mood_trend_days: clamp(env_parse("MOOD_TREND_DAYS", 7), 1, 90),
            time_awareness_enabled: env_bool("TIME_AWARENESS_ENABLED", true),

            app_port: env_parse("APP_PORT_INTERNAL", 8000),

            shutdown_timeout_seconds: clamp(env_parse("SHUTDOWN_TIMEOUT_SECONDS", 30), 1, 600),

            qq_app_id: env_string("QQ_APP_ID", ""),
            qq_app_secret: env_string("QQ_APP_SECRET", ""),
            qq_event_mode,
            qq_listen_addr: env_string("QQ_LISTEN_ADDR", ":9000"),
            qq_webhook_path,
            qq_ai_timeout_seconds: clamp(env_parse("QQ_AI_TIMEOUT_SECONDS", 180), 5, 600),
            qq_openapi_timeout_seconds: clamp(env_parse("QQ_OPENAPI_TIMEOUT_SECONDS", 15), 5, 60),
            qq_reply_max_runes: clamp(env_parse("QQ_REPLY_MAX_RUNES", 1800), 200, 10000),
            qq_reply_max_parts: clamp(env_parse("QQ_REPLY_MAX_PARTS", 4), 1, 5),
            qq_workers: clamp(env_parse("QQ_WORKERS", 8), 1, 64),
            qq_queue_size: clamp(env_parse("QQ_QUEUE_SIZE", 128), 1, 10000),
            qq_dedup_ttl_seconds: clamp(env_parse("QQ_DEDUP_TTL_SECONDS", 600), 60, 86400),
            qq_max_webhook_bytes: clamp(env_parse("QQ_MAX_WEBHOOK_BYTES", 1048576), 4096, 10485760),
        })
    }

    /// /health 与 /v1/config 暴露的脱敏配置摘要。
    pub fn safe_summary(&self) -> serde_json::Value {
        #[derive(Serialize)]
        struct Summary<'a> {
            ai_base_url: &'a str,
            memory_base_url: &'a str,
            memory_model: &'a str,
            chat_model: &'a str,
            chat_think: &'a str,
            chat_thinking_levels: Vec<&'a str>,
            db_path: &'a str,
            memory_search_limit: usize,
            memory_select_pool_max: usize,
            mcp_servers: Vec<&'a str>,
        }
        serde_json::to_value(Summary {
            ai_base_url: &self.ai_base_url,
            memory_base_url: &self.memory_base_url,
            memory_model: &self.memory_model,
            chat_model: &self.chat_model,
            chat_think: self.chat_think.key(),
            chat_thinking_levels: self.ai_thinking_map.keys().map(String::as_str).collect(),
            db_path: &self.db_path,
            memory_search_limit: self.memory_search_limit,
            memory_select_pool_max: self.memory_select_pool_max,
            mcp_servers: self.mcp_servers.iter().map(|s| s.name.as_str()).collect(),
        })
        .expect("safe summary 序列化不应失败")
    }
}

fn parse_headers(raw: &str) -> Result<Vec<(String, String)>> {
    let value: serde_json::Value = serde_json::from_str(if raw.is_empty() { "{}" } else { raw })?;
    let object = value.as_object().context("需要 JSON 对象")?;
    Ok(object
        .iter()
        .map(|(k, v)| {
            let text = v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string());
            (k.clone(), text)
        })
        .collect())
}

/// 解析 *_THINKING_MAP_JSON：形如 {"high": {"reasoning_effort": "high"}, ...}。
/// 顶层 key 必须是合法思考等级（off/low/medium/high 或其别名，归一化到标准 key），
/// 对应的值必须是对象（要合并进请求体的字段片段）。
fn parse_thinking_map(raw: &str) -> Result<Map<String, Value>> {
    let value: Value = serde_json::from_str(if raw.trim().is_empty() { "{}" } else { raw })
        .context("必须是合法 JSON")?;
    let object = value.as_object().context("需要 JSON 对象")?;
    let mut map = Map::new();
    for (key, fragment) in object {
        let level = Think::parse(key)
            .with_context(|| format!("未知的思考等级 key：{key}（应为 off/low/medium/high）"))?;
        if !fragment.is_object() {
            bail!("思考等级 {key} 的值必须是对象，例如 {{\"reasoning_effort\": \"high\"}}");
        }
        // 归一化到标准 key，别名（none/minimal/max…）不至于查不到。
        if map.insert(level.key().to_string(), fragment.clone()).is_some() {
            bail!("思考等级 {} 被重复定义（注意别名会归一）", level.key());
        }
    }
    Ok(map)
}

fn parse_mcp_servers(raw: &str) -> Result<Vec<McpServer>> {
    let value: serde_json::Value =
        serde_json::from_str(raw).context("MCP_SERVERS_JSON 必须是合法 JSON")?;
    let items = value.as_array().context("MCP_SERVERS_JSON 必须是 JSON 数组")?;
    let mut servers = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for (index, item) in items.iter().enumerate() {
        let object = item
            .as_object()
            .with_context(|| format!("MCP_SERVERS_JSON[{index}] 必须是对象"))?;
        if !object.get("enabled").and_then(|v| v.as_bool()).unwrap_or(true) {
            continue;
        }
        let name = object
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        let url_raw = object.get("url").and_then(|v| v.as_str()).unwrap_or("").trim();
        if name.is_empty() || url_raw.is_empty() {
            bail!("MCP_SERVERS_JSON[{index}] 缺少 name 或 url");
        }
        if !seen.insert(name.clone()) {
            bail!("MCP_SERVERS_JSON 出现重复的 name：{name}");
        }
        if let Some(transport) = object.get("transport").and_then(|v| v.as_str()) {
            if transport != "streamable_http" {
                bail!("MCP_SERVERS_JSON[{index}] 的 transport 目前只支持 streamable_http");
            }
        }
        let url = expand_env_refs(url_raw)?;
        let mut headers = Vec::new();
        if let Some(header_map) = object.get("headers").and_then(|v| v.as_object()) {
            for (k, v) in header_map {
                let text = v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string());
                headers.push((k.clone(), expand_env_refs(&text)?));
            }
        }
        let string_list = |key: &str| -> Vec<String> {
            object
                .get(key)
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|v| v.as_str())
                        .map(|s| s.trim().to_string())
                        .filter(|s| !s.is_empty())
                        .collect()
                })
                .unwrap_or_default()
        };
        servers.push(McpServer {
            name,
            url,
            headers,
            include: string_list("tools"),
            exclude: string_list("exclude"),
        });
    }
    Ok(servers)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expand_refs_braced_and_bare() {
        std::env::set_var("MNEME_TEST_KEY", "tvly-123");
        assert_eq!(
            expand_env_refs("https://x/?k=${MNEME_TEST_KEY}&v=$MNEME_TEST_KEY/mcp").unwrap(),
            "https://x/?k=tvly-123&v=tvly-123/mcp"
        );
    }

    #[test]
    fn expand_refs_missing_var_fails() {
        assert!(expand_env_refs("${MNEME_TEST_MISSING_VAR}").is_err());
    }

    #[test]
    fn mcp_servers_parse_with_filters() {
        std::env::set_var("MNEME_TEST_KEY2", "fc-abc");
        let servers = parse_mcp_servers(
            r#"[{"name":"firecrawl","url":"https://mcp.firecrawl.dev/${MNEME_TEST_KEY2}/v2/mcp","tools":["firecrawl_scrape"]},{"name":"off","url":"https://x","enabled":false}]"#,
        )
        .unwrap();
        assert_eq!(servers.len(), 1);
        assert_eq!(servers[0].url, "https://mcp.firecrawl.dev/fc-abc/v2/mcp");
        assert_eq!(servers[0].include, vec!["firecrawl_scrape"]);
    }
}
