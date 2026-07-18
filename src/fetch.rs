//! 内置 fetch_url 工具：抓取公开网页，抽正文转 Markdown，替代外部 Firecrawl。
//! 只做「HTTP 拉取 → readability 抽正文 → HTML 转 Markdown」，不渲染 JS——
//! 覆盖静态/SSR 页面（新闻、博客、文档、GitHub 等）这类"取链接"的绝大多数场景。
//! 需要过 Cloudflare JS 墙或纯 SPA 的少数站点不在本工具目标内（那类才需要真浏览器）。

use std::net::IpAddr;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use futures_util::StreamExt;
use reqwest::redirect::Policy;
use reqwest::Url;
use serde_json::{json, Value};

/// 手动跟跳转的上限：每跳都要重做 SSRF 校验，故自己控制次数。
const MAX_REDIRECTS: usize = 5;

pub struct Fetcher {
    http: reqwest::Client,
    /// 响应体下载上限（字节），流式读取，超过即截断，避免大文件打爆内存。
    max_bytes: usize,
    /// 回传给模型的正文字符上限，超出截断。
    max_chars: usize,
}

impl Fetcher {
    pub fn new(timeout_seconds: f64, max_bytes: usize, max_chars: usize) -> Result<Self> {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs_f64(timeout_seconds))
            // 自己逐跳跟重定向并校验，禁用 reqwest 的自动跟随，防止重定向到内网。
            .redirect(Policy::none())
            .user_agent(concat!("qq-agent/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(Self {
            http,
            max_bytes,
            max_chars,
        })
    }

    /// 抓取一个 URL，返回 {url, title, text} —— text 为抽取后的 Markdown 正文。
    pub async fn fetch(&self, raw_url: &str) -> Result<Value> {
        let mut url = Url::parse(raw_url.trim()).context("URL 无法解析")?;
        let mut hops = 0usize;
        let response = loop {
            validate_public_url(&url).await?;
            let resp = self
                .http
                .get(url.clone())
                .send()
                .await
                .map_err(|e| anyhow!("请求失败：{e}"))?;
            if resp.status().is_redirection() {
                if hops >= MAX_REDIRECTS {
                    bail!("重定向次数过多");
                }
                let location = resp
                    .headers()
                    .get(reqwest::header::LOCATION)
                    .and_then(|v| v.to_str().ok())
                    .ok_or_else(|| anyhow!("重定向响应缺少 Location"))?;
                // 相对跳转按当前 URL 解析成绝对地址。
                url = url.join(location).context("重定向地址无法解析")?;
                hops += 1;
                continue;
            }
            break resp;
        };

        let status = response.status();
        if !status.is_success() {
            bail!("目标返回 HTTP {}", status.as_u16());
        }
        let final_url = response.url().clone();
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_ascii_lowercase();

        let is_html =
            content_type.contains("text/html") || content_type.contains("application/xhtml");
        // 二进制内容（pdf/图片/压缩包等）不下载正文，直接说明，省流量也避免乱码。
        let is_text = is_html
            || content_type.starts_with("text/")
            || content_type.contains("json")
            || content_type.contains("xml")
            || content_type.is_empty();
        if !is_text {
            return Ok(json!({
                "url": final_url.as_str(),
                "content_type": content_type,
                "text": format!("该链接是非文本内容（{content_type}），未抓取正文。"),
            }));
        }

        let body = read_capped(response, self.max_bytes).await?;
        // 有些服务器不给 Content-Type：正文看起来像 HTML 就也走抽正文。
        let looks_html = content_type.is_empty() && {
            let head = body.trim_start();
            let head = head.get(..head.len().min(64)).unwrap_or("").to_ascii_lowercase();
            head.contains("<html") || head.contains("<!doctype")
        };
        let (title, markdown) = if is_html || looks_html {
            extract_html(&body, final_url.as_str())
        } else {
            (String::new(), body)
        };
        let text = truncate_chars(markdown.trim(), self.max_chars);
        if text.is_empty() {
            bail!("未能从该页面提取到正文内容");
        }
        Ok(json!({
            "url": final_url.as_str(),
            "title": title,
            "text": text,
        }))
    }
}

/// readability 抽正文 → htmd 转 Markdown；抽取失败时退化为整页转换。
fn extract_html(html: &str, base_url: &str) -> (String, String) {
    match dom_smoothie::Readability::new(html, Some(base_url), None).and_then(|mut r| r.parse()) {
        Ok(article) => {
            let content_html: String = article.content.to_string();
            let markdown = htmd::convert(&content_html)
                .unwrap_or_else(|_| article.text_content.to_string());
            (article.title, markdown)
        }
        // 非文章页/结构异常：readability 拿不到正文，退化为整页转 Markdown（htmd 会跳过 script/style）。
        Err(_) => (String::new(), htmd::convert(html).unwrap_or_default()),
    }
}

/// 流式读取响应体并在 max_bytes 处截断；按 UTF-8 有损解码（非 UTF-8 页面可能少量乱码）。
async fn read_capped(response: reqwest::Response, max_bytes: usize) -> Result<String> {
    let mut stream = response.bytes_stream();
    let mut buf: Vec<u8> = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| anyhow!("读取响应体失败：{e}"))?;
        if buf.len() + chunk.len() > max_bytes {
            buf.extend_from_slice(&chunk[..max_bytes - buf.len()]);
            break;
        }
        buf.extend_from_slice(&chunk);
    }
    Ok(String::from_utf8_lossy(&buf).into_owned())
}

fn truncate_chars(text: &str, max_chars: usize) -> String {
    if text.chars().count() > max_chars {
        text.chars().take(max_chars).collect::<String>() + "…（正文过长已截断）"
    } else {
        text.to_string()
    }
}

/// SSRF 防护：只放行 http/https，且目标解析出的所有地址都必须是公网地址。
/// 拦掉 localhost、内网、链路本地、云元数据 (169.254.169.254) 等，防止模型被诱导打内网。
async fn validate_public_url(url: &Url) -> Result<()> {
    match url.scheme() {
        "http" | "https" => {}
        other => bail!("只支持 http/https 链接，收到 {other}"),
    }
    if !url.username().is_empty() || url.password().is_some() {
        bail!("URL 不允许携带用户名或密码");
    }
    let host = url.host_str().ok_or_else(|| anyhow!("URL 缺少主机名"))?;
    let port = url.port_or_known_default().unwrap_or(80);
    let mut resolved = false;
    for addr in tokio::net::lookup_host((host, port))
        .await
        .map_err(|e| anyhow!("域名解析失败：{e}"))?
    {
        resolved = true;
        if !is_global(&addr.ip()) {
            bail!("拒绝访问内网/保留地址：{}", addr.ip());
        }
    }
    if !resolved {
        bail!("域名无法解析出任何地址");
    }
    Ok(())
}

/// 判断 IP 是否为可路由的公网地址（用稳定 API 手写，避免依赖 unstable 的 is_global）。
fn is_global(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            let o = v4.octets();
            !(v4.is_private()
                || v4.is_loopback()
                || v4.is_link_local()
                || v4.is_unspecified()
                || v4.is_broadcast()
                || v4.is_documentation()
                || o[0] == 0
                || (o[0] == 100 && (o[1] & 0xc0) == 64) // 100.64.0.0/10 运营商级 NAT
                || o[0] >= 240) // 240.0.0.0/4 保留段
        }
        IpAddr::V6(v6) => {
            if v6.is_loopback() || v6.is_unspecified() {
                return false;
            }
            // IPv4-mapped（::ffff:a.b.c.d）按内嵌的 v4 判定。
            if let Some(v4) = v6.to_ipv4_mapped() {
                return is_global(&IpAddr::V4(v4));
            }
            let seg = v6.segments();
            let unique_local = (seg[0] & 0xfe00) == 0xfc00; // fc00::/7
            let link_local = (seg[0] & 0xffc0) == 0xfe80; // fe80::/10
            !(unique_local || link_local)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocks_private_and_reserved_addresses() {
        for ip in [
            "127.0.0.1",
            "10.1.2.3",
            "192.168.0.1",
            "172.16.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "::1",
            "fc00::1",
            "fe80::1",
            "::ffff:127.0.0.1",
        ] {
            assert!(!is_global(&ip.parse().unwrap()), "{ip} 应被判为非公网");
        }
    }

    #[test]
    fn allows_public_addresses() {
        for ip in ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"] {
            assert!(is_global(&ip.parse().unwrap()), "{ip} 应被判为公网");
        }
    }

    #[test]
    fn truncates_long_text() {
        let s = "字".repeat(20);
        let out = truncate_chars(&s, 5);
        assert!(out.starts_with("字字字字字…"));
    }
}
