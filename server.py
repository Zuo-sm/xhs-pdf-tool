#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书帖子 PDF 生成器后台

启动：
  python server.py

然后打开：
  http://127.0.0.1:8765
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

try:
    from PIL import Image
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Image as PdfImage
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
except ImportError as exc:
    print(f"[!] 缺少依赖: {exc}")
    print("请安装: python -m pip install pillow reportlab")
    sys.exit(1)


ROOT = Path(__file__).resolve().parent
PDF_DIR = ROOT / "xhs_pdfs"
IMAGE_DIR = PDF_DIR / "_images"
PROFILE_DIR = Path.home() / ".xhs_pdf_browser_profile"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
OPEN_BROWSER = os.environ.get("OPEN_BROWSER", "1") == "1"


@dataclass
class Comment:
    author: str = ""
    content: str = ""
    likes: int = 0
    time_text: str = ""


@dataclass
class Post:
    url: str
    title: str = "小红书帖子"
    author: str = ""
    content: str = ""
    images: list[str] | None = None
    comments: list[Comment] | None = None


def sanitize_filename(text: str, fallback: str = "xhs_post") -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = text.strip("._ ")
    return (text or fallback)[:80]


def normalize_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", (text or "").replace("\r", "\n")).strip()


def parse_like_count(text: str) -> int:
    raw = (text or "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*万", raw)
    if match:
        return int(float(match.group(1)) * 10000)
    nums = re.findall(r"\d+", raw.replace(",", ""))
    return int(nums[0]) if nums else 0


def dedupe_comments(comments: list[Comment]) -> list[Comment]:
    seen: set[str] = set()
    result: list[Comment] = []
    for comment in comments:
        key = re.sub(r"\s+", "", comment.content)[:90]
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(comment)
    return result


def parse_cookie_header(cookie_header: str) -> list[dict]:
    cookies = []
    for part in (cookie_header or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".xiaohongshu.com",
            "path": "/",
        })
    return cookies


async def apply_cookies(context, cookie_header: str) -> None:
    cookies = parse_cookie_header(cookie_header)
    if cookies:
        await context.add_cookies(cookies)


async def scroll_comments(page, rounds: int) -> None:
    for _ in range(max(1, rounds)):
        await page.mouse.wheel(0, 1700)
        await page.wait_for_timeout(850)


async def extract_post(page, url: str, max_comments: int) -> Post:
    data = await page.evaluate(
        """(maxComments) => {
            const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            const textOf = (selector) => clean(document.querySelector(selector)?.innerText || '');
            const meta = (name) => document.querySelector(`meta[property="${name}"], meta[name="${name}"]`)?.content || '';

            const title =
                textOf('#detail-title') ||
                textOf('[class*="title"]') ||
                clean(meta('og:title')) ||
                clean(document.title).replace(/ - 小红书$/, '') ||
                '小红书帖子';

            const author =
                textOf('.author .name') ||
                textOf('[class*="author"] [class*="name"]') ||
                textOf('[class*="user"] [class*="name"]') ||
                textOf('[class*="nickname"]');

            const content =
                textOf('#detail-desc') ||
                textOf('[class*="desc"]') ||
                textOf('[class*="note-content"]') ||
                clean(meta('description'));

            const images = [];
            const seenImgs = new Set();
            for (const img of Array.from(document.querySelectorAll('img'))) {
                const src = img.currentSrc || img.src || '';
                if (!src || src.startsWith('data:') || seenImgs.has(src)) continue;
                if (/avatar|icon|sprite|logo|favicon/i.test(src)) continue;
                const box = img.getBoundingClientRect();
                if (box.width < 120 || box.height < 120) continue;
                seenImgs.add(src);
                images.push(src);
                if (images.length >= 18) break;
            }

            const commentNodes = Array.from(document.querySelectorAll(
                '.comment-item, .comment, [class*="commentItem"], [class*="comment-item"], [class*="CommentItem"]'
            ));

            const comments = commentNodes.map((node) => {
                const q = (sel) => clean(node.querySelector(sel)?.innerText || '');
                const whole = clean(node.innerText || '');
                const author = q('.name') || q('.author') || q('[class*="name"]') || '';
                const content = q('.content') || q('[class*="content"]') || whole;
                const likesText = q('.like') || q('[class*="like"]') || q('[class*="interact"]') || '';
                const timeText = q('.date') || q('.time') || q('[class*="date"]') || q('[class*="time"]') || '';
                return { author, content, likesText, timeText };
            }).filter((c) => c.content && c.content.length > 1);

            return { title, author, content, images, comments: comments.slice(0, Math.max(maxComments * 5, 40)) };
        }""",
        max_comments,
    )

    comments = [
        Comment(
            author=normalize_text(item.get("author", "")),
            content=normalize_text(item.get("content", "")),
            likes=parse_like_count(item.get("likesText", "")),
            time_text=normalize_text(item.get("timeText", "")),
        )
        for item in data.get("comments", [])
    ]
    comments = dedupe_comments(comments)
    comments.sort(key=lambda item: item.likes, reverse=True)

    return Post(
        url=url,
        title=normalize_text(data.get("title", "")) or "小红书帖子",
        author=normalize_text(data.get("author", "")),
        content=normalize_text(data.get("content", "")),
        images=data.get("images", []),
        comments=comments[:max_comments],
    )


async def login(show_browser: bool = True) -> list[str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("缺少 playwright。请运行: python -m pip install playwright，然后运行: python -m playwright install chromium")

    logs = ["正在打开小红书登录页。"]
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=not show_browser,
            viewport={"width": 1360, "height": 900},
            locale="zh-CN",
        )
        page = await context.new_page()
        await page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=60000)
        logs.append("请在打开的浏览器中完成登录，登录后回到这个命令行窗口按回车。")
        await asyncio.to_thread(input)
        await context.close()
    logs.append("登录状态已保存。")
    return logs


async def scrape_and_build(
    urls: list[str],
    comments: int,
    scroll_rounds: int,
    show_browser: bool,
    cookie_header: str = "",
) -> tuple[list[str], list[Path]]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("缺少 playwright。请运行: python -m pip install playwright，然后运行: python -m playwright install chromium")

    logs: list[str] = []
    files: list[Path] = []
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=not show_browser,
            viewport={"width": 1360, "height": 900},
            locale="zh-CN",
        )
        await apply_cookies(context, cookie_header)
        for index, url in enumerate(urls, 1):
            page = await context.new_page()
            try:
                logs.append(f"[{index}/{len(urls)}] 打开: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2500)
                await scroll_comments(page, scroll_rounds)
                post = await extract_post(page, url, comments)
                logs.append(f"抓到标题: {post.title}")
                logs.append(f"图片: {len(post.images or [])} 张，评论: {len(post.comments or [])} 条")
                pdf = build_pdf(post)
                files.append(pdf)
                logs.append(f"已生成: {pdf.name}")
            except Exception as exc:
                logs.append(f"处理失败: {url} - {exc}")
            finally:
                await page.close()
        await context.close()
    return logs, files


def download_image(url: str) -> Path | None:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"
    target = IMAGE_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:18] + ext)
    if target.exists():
        return target
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=20) as resp:
            target.write_bytes(resp.read())
        if target.suffix.lower() == ".webp":
            converted = target.with_suffix(".jpg")
            Image.open(target).convert("RGB").save(converted, "JPEG", quality=90)
            return converted
        return target
    except Exception:
        return None


def register_font() -> str:
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"
    except Exception:
        return "Helvetica"


def para(text: str, style: ParagraphStyle) -> Paragraph:
    escaped = (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )
    return Paragraph(escaped, style)


def add_pdf_image(story: list, path: Path, max_width: float) -> None:
    try:
        with Image.open(path) as img:
            width_px, height_px = img.size
        width = min(max_width, 156 * mm)
        height = width * height_px / width_px
        if height > 190 * mm:
            height = 190 * mm
            width = height * width_px / height_px
        story.append(PdfImage(str(path), width=width, height=height))
        story.append(Spacer(1, 5 * mm))
    except Exception:
        return


def build_pdf(post: Post) -> Path:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_path = PDF_DIR / f"{sanitize_filename(post.title)}_{timestamp}.pdf"
    font = register_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleCN", parent=styles["Title"], fontName=font, fontSize=20, leading=27, spaceAfter=8 * mm)
    meta_style = ParagraphStyle("MetaCN", parent=styles["BodyText"], fontName=font, fontSize=9, leading=14, textColor=colors.HexColor("#666666"), spaceAfter=5 * mm)
    body_style = ParagraphStyle("BodyCN", parent=styles["BodyText"], fontName=font, fontSize=11, leading=18, spaceAfter=6 * mm)
    heading_style = ParagraphStyle("HeadingCN", parent=styles["Heading2"], fontName=font, fontSize=15, leading=22, spaceBefore=6 * mm, spaceAfter=4 * mm)
    comment_style = ParagraphStyle("CommentCN", parent=styles["BodyText"], fontName=font, fontSize=10, leading=16, leftIndent=3 * mm, spaceAfter=4 * mm)

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=post.title,
    )
    story: list = [
        para(post.title, title_style),
        para("<br/>".join([
            f"作者: {post.author or '未抓到'}",
            f"来源: {post.url}",
            f"保存时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]), meta_style),
        para("正文", heading_style),
        para(post.content or "未抓到正文。", body_style),
    ]

    image_paths = [path for src in (post.images or []) if (path := download_image(src))]
    if image_paths:
        story.append(para("图片", heading_style))
        for path in image_paths:
            add_pdf_image(story, path, doc.width)

    if post.comments:
        story.append(PageBreak())
        story.append(para(f"高赞评论 Top {len(post.comments)}", heading_style))
        for idx, comment in enumerate(post.comments, 1):
            bits = [comment.author or "匿名", f"{comment.likes} 赞"]
            if comment.time_text:
                bits.append(comment.time_text)
            story.append(para(f"{idx}. " + " / ".join(bits), meta_style))
            story.append(para(comment.content, comment_style))
    else:
        story.append(para("高赞评论", heading_style))
        story.append(para("未抓到可见评论。请确认已登录，并尝试增加滚动次数。", body_style))

    doc.build(story)
    return pdf_path


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/health":
            self.send_json({"ok": True})
            return
        if path.startswith("/pdfs/"):
            target = (PDF_DIR / path.removeprefix("/pdfs/")).resolve()
            if not str(target).startswith(str(PDF_DIR.resolve())) or not target.exists():
                self.send_error(404)
                return
            self.serve_file(target)
            return
        target = ROOT / "index.html" if path in {"/", "/index.html"} else ROOT / path.lstrip("/")
        if not target.exists() or not target.is_file():
            self.send_error(404)
            return
        self.serve_file(target)

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
            if self.path == "/api/login":
                logs = asyncio.run(login(bool(payload.get("showBrowser", True))))
                self.send_json({"ok": True, "logs": logs})
                return
            if self.path == "/api/generate":
                urls = [str(x).strip() for x in payload.get("urls", []) if str(x).strip()]
                if not urls:
                    self.send_json({"ok": False, "error": "请提供帖子链接"}, 400)
                    return
                logs, files = asyncio.run(scrape_and_build(
                    urls=urls,
                    comments=int(payload.get("comments", 10)),
                    scroll_rounds=int(payload.get("scrollRounds", 10)),
                    show_browser=bool(payload.get("showBrowser", True)),
                    cookie_header=str(payload.get("cookieHeader", "")),
                ))
                self.send_json({
                    "ok": True,
                    "logs": logs,
                    "files": [{"name": f.name, "url": f"/pdfs/{f.name}"} for f in files],
                })
                return
            self.send_error(404)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)

    def serve_file(self, target: Path) -> None:
        data = target.read_bytes()
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    visible_host = "127.0.0.1" if HOST == "0.0.0.0" else HOST
    url = f"http://{visible_host}:{PORT}"
    print(f"小红书帖子 PDF 生成器已启动: {url}")
    print("本地使用可点“先登录小红书”；云端部署可填写 Cookie 或抓取公开可见内容。")
    if OPEN_BROWSER and HOST in {"127.0.0.1", "localhost"}:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    server.serve_forever()


if __name__ == "__main__":
    main()
