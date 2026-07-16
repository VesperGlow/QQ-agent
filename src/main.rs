//! QQ Agent：单进程二进制 —— HTTP API + 进程内 embedding + SQLite 分级记忆 + QQ 桥接。

mod agent;
mod api;
mod config;
mod embedding;
mod llm;
mod mcp;
mod qq;
mod store;

use std::sync::Arc;

use anyhow::{Context, Result};

#[tokio::main]
async fn main() -> Result<()> {
    let cfg = Arc::new(config::Config::from_env()?);
    let level = cfg.log_level.to_lowercase();
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_new(format!(
                "{level},hyper=warn,reqwest=warn,tungstenite=warn,ort=warn"
            ))
            .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let store = store::Store::open(cfg.clone())?;
    let embedder = Arc::new(embedding::Embedder::new(cfg.clone())?);
    let llm = Arc::new(llm::LlmClient::new(cfg.clone())?);
    let mut mcp = mcp::McpManager::new(cfg.clone())?;
    mcp.start().await?;
    let agent = agent::Agent::new(cfg.clone(), store, embedder.clone(), llm, Arc::new(mcp));

    // 预热本地 embedding（首次启动含模型下载），不阻塞服务就绪。
    {
        let embedder = embedder.clone();
        tokio::spawn(async move {
            if let Err(error) = embedder.warmup().await {
                tracing::warn!("Embedding 预热失败：{error:#}");
            }
        });
    }

    // HTTP API
    let state = api::AppState {
        cfg: cfg.clone(),
        agent: agent.clone(),
    };
    let api_addr = format!("0.0.0.0:{}", cfg.app_port);
    let listener = tokio::net::TcpListener::bind(&api_addr)
        .await
        .with_context(|| format!("监听 {api_addr} 失败"))?;
    tracing::info!("AI API 已启动: http://{api_addr}");
    let api_server = tokio::spawn(async move { axum::serve(listener, api::router(state)).await });

    // QQ 桥接（与 API 同进程；任一退出即整体退出，交给容器 restart 拉起）
    let bridge = qq::QqBridge::new(cfg.clone(), agent)?;
    tokio::select! {
        result = api_server => {
            result??;
            anyhow::bail!("AI API 服务意外退出");
        }
        result = bridge.run() => {
            result?;
            anyhow::bail!("QQ 桥接意外退出");
        }
    }
}
