//! 一次性子命令（如 `mneme memory list`）：不启动服务，直接查/改 SQLite。
//! 供 `podman exec <容器> mneme memory list` 这类运维排查用。

use anyhow::{anyhow, bail, Result};

use crate::config::Config;
use crate::store::{self, ListFilter};

const USAGE: &str = "\
用法：
  mneme memory list [--user <id>] [--limit N] [--all] [--json]
  mneme memory delete <id> [<id> ...]
  mneme memory delete --all --yes
  mneme memory stats [--user <id>] [--json]

选项（list）：
  -u, --user <id>   只看某个用户（如 qq:c2c:xxxx）
  -n, --limit N     最多列出多少条（默认 200）
  -a, --all         包含已失效的记忆（被删除/被取代）
  -j, --json        输出 JSON（含 id / 时间戳等完整字段）

delete 软删除：active=0 并移出向量索引（检索不到、库里留痕，list --all 仍可见）；
       可一次给多个 id/前缀；--all 删全部活跃记忆、需 --yes 确认。
stats  按活跃/失效、类型汇总条数（只读）。";

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
        Some("delete") => memory_delete(cfg, &args[1..]),
        Some("stats") => memory_stats(cfg, &args[1..]),
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
    // 单人库（或已按用户过滤）时不逐行重复用户，只在标题里带一次。
    let users: std::collections::BTreeSet<&str> = rows.iter().map(|r| r.user_id.as_str()).collect();
    let per_row_user = filter.user_id.is_none() && users.len() > 1;
    match &filter.user_id {
        Some(uid) => println!("共 {} 条{}，user={uid}：", rows.len(), scope),
        None if users.len() == 1 => {
            println!("共 {} 条{}，user={}：", rows.len(), scope, users.iter().next().unwrap())
        }
        None => println!("共 {} 条{}（{} 个用户）：", rows.len(), scope, users.len()),
    }
    for row in &rows {
        // active 用 ✓/✗，日期只留到秒，text 放最后免去 CJK 等宽对齐问题。
        let flag = if row.active { '✓' } else { '✗' };
        let when = row.created_at.get(0..19).unwrap_or(&row.created_at);
        let text = truncate(&row.text, 60);
        let mut line = format!(
            "{flag} {when}  {:<11} ×{}",
            row.kind, row.repetitions
        );
        // 多用户时才附用户尾号区分；id 只显示 8 位前缀（delete 认前缀）。
        if per_row_user {
            line.push_str(&format!("  [{}]", user_tail(&row.user_id)));
        }
        line.push_str(&format!("  {}  {}", id_head(&row.id), text));
        println!("{line}");
    }
    let example = rows.first().map(|r| id_head(&r.id)).unwrap_or_default();
    println!("\n（delete 用前缀即可，如 mneme memory delete {example}）");
    Ok(())
}

fn memory_delete(cfg: &Config, args: &[String]) -> Result<()> {
    let mut ids: Vec<String> = Vec::new();
    let mut all = false;
    let mut yes = false;
    for arg in args {
        match arg.as_str() {
            "--all" | "-a" => all = true,
            "--yes" | "-y" => yes = true,
            "--help" | "-h" => {
                println!("{USAGE}");
                return Ok(());
            }
            other if other.starts_with('-') => bail!("未知参数：{other}\n\n{USAGE}"),
            other => ids.push(other.to_string()),
        }
    }

    // 删除全部：危险，强制 --yes 确认。
    if all {
        if !ids.is_empty() {
            bail!("--all 不能和具体 id 混用");
        }
        if !yes {
            bail!("这会软删除全部活跃记忆（active=0，list --all 仍可见）。确认请加 --yes：\n  mneme memory delete --all --yes");
        }
        let n = store::cli_delete_all(cfg)?;
        println!("已软删除 {n} 条记忆（active=0，list --all 仍可见）。");
        return Ok(());
    }

    if ids.is_empty() {
        bail!("需要一个或多个记忆 id（或 --all）\n\n{USAGE}");
    }

    let mut done = 0usize;
    for id in &ids {
        match store::cli_delete_memory(cfg, id) {
            Ok(Some(text)) => {
                done += 1;
                println!("✓ 已删除 {id}：{}", truncate(&text, 80));
            }
            Ok(None) => println!("✗ 未找到活跃记忆：{id}"),
            // 前缀歧义等错误只影响这一条，不中断整批。
            Err(error) => println!("✗ {id}：{error}"),
        }
    }
    if ids.len() > 1 {
        println!("\n完成：{done}/{} 条。", ids.len());
    }
    Ok(())
}

fn memory_stats(cfg: &Config, args: &[String]) -> Result<()> {
    let mut user: Option<String> = None;
    let mut as_json = false;
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--json" | "-j" => as_json = true,
            "--user" | "-u" => {
                user = Some(it.next().ok_or_else(|| anyhow!("--user 需要一个值"))?.clone());
            }
            "--help" | "-h" => {
                println!("{USAGE}");
                return Ok(());
            }
            other => bail!("未知参数：{other}\n\n{USAGE}"),
        }
    }

    let s = store::cli_stats(cfg, user.as_deref())?;
    if as_json {
        println!("{}", serde_json::to_string_pretty(&s)?);
        return Ok(());
    }

    println!(
        "活跃 {} · 已失效 {} · 合计 {}（{} 个用户）",
        s.active, s.inactive, s.total, s.users
    );
    if s.active == 0 {
        println!("（无活跃记忆）");
        return Ok(());
    }
    let kinds = s
        .by_kind
        .iter()
        .map(|(k, c)| format!("{k}×{c}"))
        .collect::<Vec<_>>()
        .join("  ");
    println!("类型：{kinds}");
    if let (Some(o), Some(n)) = (&s.oldest, &s.newest) {
        println!("跨度：{} ~ {}", o.get(0..10).unwrap_or(o), n.get(0..10).unwrap_or(n));
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

/// user_id 取尾 8 字符，便于多用户时人眼区分而不刷屏。
fn user_tail(value: &str) -> String {
    let chars: Vec<char> = value.chars().collect();
    if chars.len() <= 10 {
        value.to_string()
    } else {
        format!("…{}", chars[chars.len() - 8..].iter().collect::<String>())
    }
}

/// 记忆 id 取前 8 字符（UUID 前缀，个人库唯一性足够）；delete 认前缀。
fn id_head(id: &str) -> String {
    id.chars().take(8).collect()
}
