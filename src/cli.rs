//! 一次性子命令（如 `qq-agent memory list`）：不启动服务，直接查 SQLite。
//! 供 `podman exec <容器> qq-agent memory list` 这类运维排查用。

use anyhow::{anyhow, bail, Result};

use crate::config::Config;
use crate::store::{self, ListFilter};

const USAGE: &str = "\
用法：
  qq-agent memory list [--user <id>] [--limit N] [--all] [--json]

选项：
  -u, --user <id>   只看某个用户（如 qq:c2c:xxxx）
  -n, --limit N     最多列出多少条（默认 200）
  -a, --all         包含已失效的记忆（被遗忘/被取代）
  -j, --json        输出 JSON（含 id / 时间戳 / 过期时间等完整字段）";

/// 分发子命令。args 不含程序名（即 std::env::args().skip(1)）。
pub fn run(cfg: &Config, args: &[String]) -> Result<()> {
    match args.first().map(String::as_str) {
        Some("memory") => memory(cfg, &args[1..]),
        Some("--help" | "-h" | "help") => {
            println!("{USAGE}");
            Ok(())
        }
        Some(other) => bail!("未知子命令：{other}\n\n{USAGE}"),
        None => unreachable!("run 仅在有参数时被调用"),
    }
}

fn memory(cfg: &Config, args: &[String]) -> Result<()> {
    match args.first().map(String::as_str) {
        Some("list") => memory_list(cfg, &args[1..]),
        Some(other) => bail!("未知 memory 子命令：{other}\n\n{USAGE}"),
        None => bail!("memory 需要一个动作\n\n{USAGE}"),
    }
}

fn memory_list(cfg: &Config, args: &[String]) -> Result<()> {
    let mut filter = ListFilter {
        user_id: None,
        include_inactive: false,
        limit: 200,
    };
    let mut as_json = false;

    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--all" | "-a" => filter.include_inactive = true,
            "--json" | "-j" => as_json = true,
            "--user" | "-u" => {
                let value = it.next().ok_or_else(|| anyhow!("--user 需要一个值"))?;
                filter.user_id = Some(value.clone());
            }
            "--limit" | "-n" => {
                let value = it.next().ok_or_else(|| anyhow!("--limit 需要一个值"))?;
                filter.limit = value
                    .parse()
                    .map_err(|_| anyhow!("--limit 必须是数字：{value}"))?;
            }
            "--help" | "-h" => {
                println!("{USAGE}");
                return Ok(());
            }
            other => bail!("未知参数：{other}\n\n{USAGE}"),
        }
    }

    let rows = store::cli_list_memories(cfg, &filter)?;

    if as_json {
        println!("{}", serde_json::to_string_pretty(&rows)?);
        return Ok(());
    }
    if rows.is_empty() {
        println!("（没有记忆）");
        return Ok(());
    }

    let scope = if filter.include_inactive { "（含已失效）" } else { "" };
    match &filter.user_id {
        Some(uid) => println!("共 {} 条{}，user={uid}：", rows.len(), scope),
        None => println!("共 {} 条{}：", rows.len(), scope),
    }
    for row in &rows {
        // active 用 ✓/✗，日期只留到秒，text 放最后免去 CJK 等宽对齐问题。
        let flag = if row.active { '✓' } else { '✗' };
        let when = row.created_at.get(0..19).unwrap_or(&row.created_at);
        let text = truncate(&row.text, 60);
        let mut line = format!(
            "{flag} {when}  L{:<2} {:<11} ×{}",
            row.level, row.kind, row.repetitions
        );
        // 只在不按用户过滤时附上用户尾号，避免每行重复同一 id。
        if filter.user_id.is_none() {
            line.push_str(&format!("  [{}]", short_user(&row.user_id)));
        }
        line.push_str("  ");
        line.push_str(&text);
        println!("{line}");
    }
    Ok(())
}

/// 按字符数截断并加省略号（避免在多字节字符中间切断）。
fn truncate(text: &str, max_chars: usize) -> String {
    let flat = text.replace('\n', " ");
    if flat.chars().count() <= max_chars {
        return flat;
    }
    let mut out: String = flat.chars().take(max_chars).collect();
    out.push('…');
    out
}

/// user_id 尾 8 个字符，便于人眼区分而不刷屏。
fn short_user(user_id: &str) -> String {
    let chars: Vec<char> = user_id.chars().collect();
    if chars.len() <= 10 {
        user_id.to_string()
    } else {
        format!("…{}", chars[chars.len() - 8..].iter().collect::<String>())
    }
}
