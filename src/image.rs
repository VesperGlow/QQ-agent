//! 图片输入处理：魔数嗅探 MIME、构造 data URI、归一化 API 传入的图片参数，
//! 以及把超大图压缩进上游 vision 接口的大小限制（解码 → 缩放 → 重编码 JPEG）。
//! 图片只在当轮以 OpenAI vision 的 image_url 段传给模型，不落库。

use std::io::Cursor;

use anyhow::{bail, Context, Result};
use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use image::imageops::FilterType;
use image::{DynamicImage, RgbImage};

/// 按文件魔数识别常见图片格式；识别不了按 JPEG 处理（上游通常也能容错）。
pub fn sniff_mime(bytes: &[u8]) -> &'static str {
    if bytes.starts_with(b"\xFF\xD8\xFF") {
        "image/jpeg"
    } else if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        "image/png"
    } else if bytes.starts_with(b"GIF8") {
        "image/gif"
    } else if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WEBP" {
        "image/webp"
    } else if bytes.starts_with(b"BM") {
        "image/bmp"
    } else {
        "image/jpeg"
    }
}

pub fn to_data_uri(bytes: &[u8]) -> String {
    format!("data:{};base64,{}", sniff_mime(bytes), BASE64.encode(bytes))
}

/// 解码期允许的分配上限相对「像素数 × 每像素字节」的倍数。
///
/// 最坏路径是带透明通道的图：`DynamicImage` 本身 4 字节/像素，`flatten_to_rgb` 里
/// `to_rgba8()` 再复制一份 4 字节/像素，输出的 `RgbImage` 3 字节/像素——同时在世约
/// 11 字节/像素。取 12 留一点余量。（缩放的输出受 `max_edge` 约束，不参与这个峰值。）
const DECODE_BYTES_PER_PIXEL: u64 = 12;

/// 把图片整理到可发送状态：体积在 `max_bytes` 内且长边不超过 `max_edge` 的原样返回；
/// 否则解码（jpeg/png）、透明底合成白色、缩放，再按质量/尺寸递降重编码 JPEG，
/// 直到压进限制。解码是 CPU 密集操作，异步上下文里请放 spawn_blocking 调用。
///
/// `max_pixels` 是**解码前**的像素数闸门。`max_edge` 管的是输出尺寸，而缩放发生在解码
/// 之后——不设这道闸，一张 8000×6000 的原图会先老老实实解成几百 MB 的缓冲，容器的
/// 内存上限只能以 OOM 收场。宁可给一条能看懂的错误。
pub fn prepare(bytes: Vec<u8>, max_bytes: usize, max_edge: u32, max_pixels: u32) -> Result<Vec<u8>> {
    // 只解析一次文件头拿尺寸（原先为了同一个信息建了两次 reader）。
    let dimensions = image::ImageReader::new(Cursor::new(&bytes))
        .with_guessed_format()
        .ok()
        .and_then(|reader| reader.into_dimensions().ok());

    let needs_work = bytes.len() > max_bytes
        // 读不出尺寸（不支持的格式等）：体积达标就原样放行
        || dimensions.map_or(false, |(width, height)| width.max(height) > max_edge);
    if !needs_work {
        return Ok(bytes);
    }

    if let Some((width, height)) = dimensions {
        let pixels = u64::from(width) * u64::from(height);
        if pixels > u64::from(max_pixels) {
            bail!(
                "图片 {width}×{height} 共 {:.1} 百万像素，超过上限 {:.1} 百万（CHAT_IMAGE_MAX_PIXELS）；\
                 解码需要约 {} MB 内存，已拒绝",
                pixels as f64 / 1e6,
                f64::from(max_pixels) / 1e6,
                pixels * DECODE_BYTES_PER_PIXEL / 1_048_576,
            );
        }
    }

    let mut reader = image::ImageReader::new(Cursor::new(&bytes))
        .with_guessed_format()
        .context("识别图片格式失败")?;
    // 上面的像素闸门依赖文件头里的尺寸；读不出尺寸、或头部尺寸与实际数据不符的格式
    // 就绕过了它。这里再让解码器自己守住分配上限——image 的默认值是 512 MiB，
    // 对一个几百 MB 上限的容器等于没有。
    let mut limits = image::Limits::default();
    limits.max_alloc = Some(u64::from(max_pixels) * DECODE_BYTES_PER_PIXEL);
    reader.limits(limits);

    let decoded = match reader.decode() {
        Ok(img) => img,
        // 解码不了（gif/webp 未启用、文件损坏、超出 limits 等）：小图原样放行，大图只能拒绝
        Err(error) => {
            if bytes.len() <= max_bytes {
                return Ok(bytes);
            }
            bail!("图片超过 {max_bytes} 字节且无法解码压缩：{error}");
        }
    };

    let mut rgb = flatten_to_rgb(decoded);
    let long_edge = rgb.width().max(rgb.height());
    if long_edge > max_edge {
        rgb = scale_to(&rgb, max_edge as f32 / long_edge as f32);
    }

    // 先降质量、再缩尺寸，交替进行直到达标；尺寸单调递减保证循环终止。
    let mut quality: u8 = 85;
    loop {
        let mut out = Vec::new();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut out, quality)
            .encode_image(&rgb)
            .context("JPEG 编码失败")?;
        if out.len() <= max_bytes {
            return Ok(out);
        }
        if quality > 55 {
            quality -= 15;
            continue;
        }
        if rgb.width().min(rgb.height()) <= 200 {
            bail!("图片压缩到最低质量与尺寸后仍超过 {max_bytes} 字节");
        }
        rgb = scale_to(&rgb, 0.7);
        quality = 75;
    }
}

fn scale_to(rgb: &RgbImage, factor: f32) -> RgbImage {
    let width = ((rgb.width() as f32 * factor) as u32).max(1);
    let height = ((rgb.height() as f32 * factor) as u32).max(1);
    image::imageops::resize(rgb, width, height, FilterType::Triangle)
}

/// 展平为 RGB；带透明通道的按 alpha 合成到白色背景（JPEG 不支持透明）。
fn flatten_to_rgb(img: DynamicImage) -> RgbImage {
    match img {
        DynamicImage::ImageRgb8(rgb) => rgb,
        other => {
            let rgba = other.to_rgba8();
            let mut out = RgbImage::new(rgba.width(), rgba.height());
            for (dst, src) in out.pixels_mut().zip(rgba.pixels()) {
                let alpha = src[3] as u32;
                for channel in 0..3 {
                    dst[channel] =
                        ((src[channel] as u32 * alpha + 255 * (255 - alpha)) / 255) as u8;
                }
            }
            out
        }
    }
}

/// 归一化 API 请求里的一项图片：
/// - `http(s)://…` 原样透传（由上游 AI 提供商拉取，无法本地压缩）；
/// - `data:…;base64,…` 或裸 base64：解码校验、按需压缩，再按嗅探出的 MIME 重建 data URI。
pub fn normalize_input(
    raw: &str,
    max_bytes: usize,
    max_edge: u32,
    max_pixels: u32,
) -> Result<String> {
    let value = raw.trim();
    if value.is_empty() {
        bail!("images 里包含空项");
    }
    if value.starts_with("http://") || value.starts_with("https://") {
        return Ok(value.to_string());
    }
    let payload = match value.strip_prefix("data:") {
        Some(rest) => {
            let (meta, data) = rest.split_once(',').context("data URI 缺少逗号分隔")?;
            if !meta.ends_with(";base64") {
                bail!("data URI 必须是 base64 编码");
            }
            data
        }
        None => value,
    };
    // 宽容常见的换行/空白（有些客户端会给 base64 分行）。
    let compact: String = payload.chars().filter(|c| !c.is_whitespace()).collect();
    let bytes = BASE64
        .decode(compact.as_bytes())
        .context("图片不是合法的 base64")?;
    if bytes.is_empty() {
        bail!("图片内容为空");
    }
    let prepared = prepare(bytes, max_bytes, max_edge, max_pixels)?;
    Ok(to_data_uri(&prepared))
}

#[cfg(test)]
mod tests {
    use super::*;

    const PNG_HEAD: &[u8] = b"\x89PNG\r\n\x1a\n_rest_of_file";

    /// 生成一张带渐变噪点的测试 PNG（纯色图压缩率太高，测不出递降逻辑）。
    fn test_png(width: u32, height: u32) -> Vec<u8> {
        let img = RgbImage::from_fn(width, height, |x, y| {
            image::Rgb([
                (x * 7 % 256) as u8,
                (y * 13 % 256) as u8,
                ((x ^ y) % 256) as u8,
            ])
        });
        let mut out = Vec::new();
        DynamicImage::ImageRgb8(img)
            .write_to(&mut Cursor::new(&mut out), image::ImageFormat::Png)
            .unwrap();
        out
    }

    #[test]
    fn sniff_common_formats() {
        assert_eq!(sniff_mime(b"\xFF\xD8\xFF\xE0xxxx"), "image/jpeg");
        assert_eq!(sniff_mime(PNG_HEAD), "image/png");
        assert_eq!(sniff_mime(b"GIF89a"), "image/gif");
        assert_eq!(sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp");
        assert_eq!(sniff_mime(b"unknown"), "image/jpeg");
    }

    #[test]
    fn prepare_passes_small_images_untouched() {
        let png = test_png(64, 64);
        let out = prepare(png.clone(), 5_000_000, 2048, 16_000_000).unwrap();
        assert_eq!(out, png);
    }

    #[test]
    fn prepare_downscales_oversized_edge() {
        let png = test_png(800, 400);
        let out = prepare(png, 5_000_000, 256, 16_000_000).unwrap();
        assert!(out.starts_with(b"\xFF\xD8\xFF")); // 重编码成 JPEG
        let (width, height) = image::ImageReader::new(Cursor::new(&out))
            .with_guessed_format()
            .unwrap()
            .into_dimensions()
            .unwrap();
        assert!(width.max(height) <= 256);
        assert_eq!(width, 256);
        assert_eq!(height, 128);
    }

    #[test]
    fn prepare_shrinks_to_byte_limit() {
        let png = test_png(1000, 750);
        let limit = 60_000;
        assert!(png.len() > limit, "测试图应大于限制");
        let out = prepare(png, limit, 4096, 16_000_000).unwrap();
        assert!(out.len() <= limit, "压缩后 {} 字节仍超限", out.len());
        assert!(out.starts_with(b"\xFF\xD8\xFF"));
    }

    #[test]
    fn prepare_rejects_oversized_garbage() {
        let garbage = vec![0xABu8; 1024];
        // 体积达标：原样放行
        assert_eq!(prepare(garbage.clone(), 4096, 2048, 16_000_000).unwrap(), garbage);
        // 超限且无法解码：拒绝
        assert!(prepare(garbage, 512, 2048, 16_000_000).is_err());
    }

    #[test]
    fn prepare_rejects_images_over_pixel_budget() {
        // 800×400 = 320k 像素；闸门设 100k，且长边超过 max_edge 触发处理路径。
        let png = test_png(800, 400);
        let error = prepare(png, 5_000_000, 256, 100_000).unwrap_err().to_string();
        assert!(error.contains("800×400"), "错误信息应带上尺寸：{error}");
        assert!(error.contains("CHAT_IMAGE_MAX_PIXELS"), "应指明是哪个配置项：{error}");
    }

    #[test]
    fn pixel_budget_does_not_reject_images_that_need_no_work() {
        // 体积与长边都达标 → 走原样放行的早退分支，根本不解码，
        // 所以哪怕像素闸门设得比图还小也不该拒。顺序不能反。
        let png = test_png(800, 400);
        let out = prepare(png.clone(), 5_000_000, 2048, 1_000).unwrap();
        assert_eq!(out, png);
    }

    #[test]
    fn normalize_passes_urls_and_rebuilds_base64() {
        assert_eq!(
            normalize_input("https://example.com/a.jpg", 1024, 2048, 16_000_000).unwrap(),
            "https://example.com/a.jpg"
        );
        let encoded = BASE64.encode(PNG_HEAD);
        let uri = normalize_input(&encoded, 1024, 2048, 16_000_000).unwrap();
        assert!(uri.starts_with("data:image/png;base64,"));
        // data URI 输入按嗅探结果重建 MIME（声明的 image/jpeg 被纠正为 png）。
        let uri = normalize_input(&format!("data:image/jpeg;base64,{encoded}"), 1024, 2048, 16_000_000).unwrap();
        assert!(uri.starts_with("data:image/png;base64,"));
    }

    #[test]
    fn normalize_compresses_oversized_base64() {
        let png = test_png(1000, 750);
        let limit = 60_000;
        let uri = normalize_input(&BASE64.encode(&png), limit, 4096, 16_000_000).unwrap();
        assert!(uri.starts_with("data:image/jpeg;base64,"));
        // data URI 开销约 4/3，宽松校验落在限制附近
        assert!(uri.len() <= limit * 4 / 3 + 64);
    }

    #[test]
    fn normalize_rejects_bad_input() {
        assert!(normalize_input("", 1024, 2048, 16_000_000).is_err());
        assert!(normalize_input("不是base64!!!", 1024, 2048, 16_000_000).is_err());
        assert!(normalize_input("data:image/png,notbase64", 1024, 2048, 16_000_000).is_err()); // 非 base64 data URI
    }
}
