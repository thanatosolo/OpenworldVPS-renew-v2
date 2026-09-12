#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import io
import urllib.parse
import requests
import time
from datetime import datetime, timedelta, timezone
from PIL import Image
from playwright.sync_api import sync_playwright

# ================= 配置区 =================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME", "未命名账号")
SITE_BASE = "https://openworld.eu.org"
RENEW_THRESHOLD_DAYS = 5
# ==========================================

SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", ".")


def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    full_message = f"👤 账号: {ACCOUNT_NAME}\n{message}"
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": full_message}, timeout=10)
        if resp.status_code == 200:
            print("✅ Telegram 通知已发送")
        else:
            print(f"❌ Telegram 发送失败: HTTP {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        print(f"❌ Telegram 发送异常: {e}")


def save_screenshot(page, name: str):
    try:
        path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
        page.screenshot(path=path, full_page=False)
        print(f"   📸 截图已保存: {path}")
    except Exception as e:
        print(f"   ⚠️ 截图失败: {e}")


def dump_page_debug(page, name: str):
    """保存页面截图 + HTML + 所有 img 元素信息，用于调试"""
    save_screenshot(page, name)
    try:
        html_path = os.path.join(SCREENSHOT_DIR, f"{name}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
        print(f"   📄 HTML 已保存: {html_path}")
    except Exception as e:
        print(f"   ⚠️ HTML 保存失败: {e}")

    # 打印所有 img 元素
    try:
        imgs = page.locator("img").all()
        print(f"   🔎 页面共有 {len(imgs)} 个 img 元素:")
        for i, img in enumerate(imgs[:30]):
            try:
                src = img.get_attribute("src") or ""
                alt = img.get_attribute("alt") or ""
                cls = img.get_attribute("class") or ""
                eid = img.get_attribute("id") or ""
                if src or alt:
                    print(f"      [{i}] id='{eid}' alt='{alt}' class='{cls}' src='{src[:90]}'")
            except Exception:
                continue
    except Exception as e:
        print(f"   ⚠️ 枚举 img 失败: {e}")

    # 打印所有 canvas 元素
    try:
        canvases = page.locator("canvas").all()
        if canvases:
            print(f"   🔎 页面共有 {len(canvases)} 个 canvas 元素")
    except Exception:
        pass


def wait_for_cloudflare(page, timeout=15):
    cf_indicators = ["verify you are human", "just a moment", "checking your browser",
                     "cf-browser-verification", "challenge-platform"]
    start = time.time()
    while time.time() - start < timeout:
        try:
            content = page.content().lower()
            if not any(indicator in content for indicator in cf_indicators):
                return True
        except Exception:
            pass
        time.sleep(1)
    print("⚠️ Cloudflare 挑战等待超时")
    return False


def try_click_turnstile(page):
    """尝试点击 Turnstile 复选框（iframe 内）。返回是否进行了点击。"""
    clicked = False
    try:
        for frame in page.frames:
            furl = frame.url or ""
            if "challenges.cloudflare.com" in furl or "turnstile" in furl:
                try:
                    frame.wait_for_selector(
                        ".ctp-checkbox-label, input[type='checkbox'], label",
                        timeout=4000)
                except Exception:
                    continue
                for sel in (".ctp-checkbox-label", "input[type='checkbox']", "label"):
                    try:
                        frame.locator(sel).first.click(timeout=1500)
                        clicked = True
                        break
                    except Exception:
                        continue
    except Exception:
        pass
    return clicked


def handle_turnstile_and_finish_login(page, timeout=60):
    """
    等待 Clerk SSO 回调真正完成（登录写 Cookie）。
    Clerk 的 accounts.openworld.eu.org 回调页会先弹 Cloudflare Turnstile
    人机验证（Verify you are human），必须等它通过后才会跳回主站。
    等待期间尝试自动点击 Turnstile 复选框，并轮询直到跳回 openworld.eu.org。
    """
    print("   ⏳ 等待 Clerk/Cloudflare Turnstile 验证完成...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 1) 若存在 Turnstile iframe 复选框，尝试点击（失败忽略，部分情况会自动通过）
        try_click_turnstile(page)

        # 2) 是否已跳回主站（登录完成）
        url = page.url or ""
        if url.startswith("https://openworld.eu.org"):
            return True, url
        page.wait_for_timeout(1000)

    return False, (page.url or "")


def login_with_discord_token(page, dc_token: str) -> bool:
    print("=" * 50)
    print("🔑 开始 Discord OAuth 登录流程")
    print("=" * 50)

    print(f"\n📌 第1步：访问首页建立基础 Cookie/Session")
    try:
        page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
        wait_for_cloudflare(page)
        time.sleep(2)
        print(f"   首页加载完成，当前 URL: {page.url}")
    except Exception as e:
        print(f"   ⚠️ 首页加载异常: {e}")

    login_url = f"{SITE_BASE}/login"
    print(f"\n📌 第2步：访问登录页: {login_url}")
    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
        wait_for_cloudflare(page)
        time.sleep(3)
        print(f"   登录页加载完成，当前 URL: {page.url}")
    except Exception as e:
        print(f"   ⚠️ 登录页加载异常: {e}")

    current_url = page.url
    print(f"   当前 URL: {current_url}")

    print(f"\n📌 第3步：检查是否到达 Discord OAuth 页面")

    if "discord.com" not in current_url:
        print("   未自动跳转，尝试点击登录按钮...")
        btn_selectors = [
            "button:has-text('Sign in')",
            "a:has-text('Sign in')",
            "button:has-text('Discord')",
            "a:has-text('Discord')",
            "button:has-text('登录')",
            "a:has-text('登录')",
            "[class*='discord']",
            "[class*='login']",
            "button[type='submit']",
        ]

        for selector in btn_selectors:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=3000):
                    btn_text = btn.inner_text().strip()
                    print(f"   找到按钮: '{btn_text}' (选择器: {selector})，点击...")
                    btn.click()
                    time.sleep(5)
                    current_url = page.url
                    print(f"   点击后 URL: {current_url}")
                    if "discord.com" in current_url:
                        break
            except Exception:
                continue

        if "discord.com" not in current_url:
            print("   点击按钮未跳转，尝试从页面源码提取 OAuth 链接...")
            try:
                page_html = page.content()
                oauth_match = re.search(
                    r'https://discord\.com/oauth2/authorize[^\s"\'<>]+',
                    page_html
                )
                if oauth_match:
                    direct_url = oauth_match.group(0)
                    print(f"   找到 OAuth 链接: {direct_url[:80]}...")
                    page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(3)
                    current_url = page.url
                    print(f"   导航后 URL: {current_url}")
            except Exception as e:
                print(f"   ⚠️ 源码提取 OAuth 链接失败: {e}")

    if "discord.com" not in current_url:
        print("   等待可能的延迟重定向...")
        for i in range(10):
            time.sleep(1)
            current_url = page.url
            if "discord.com" in current_url:
                break

    if "discord.com" not in current_url:
        print(f"   ❌ 无法跳转到 Discord 授权页面")
        print(f"   当前 URL: {current_url}")
        print(f"   页面标题: {page.title()}")
        save_screenshot(page, "login_failed_no_discord")
        return False

    print(f"   ✅ 已到达 Discord OAuth 页面")

    print(f"\n📌 第4步：解析 OAuth 参数")
    oauth_url = page.url
    print(f"   当前 Discord URL: {oauth_url[:120]}...")

    if "discord.com/login" in oauth_url and "redirect_to=" in oauth_url:
        parsed_login = urllib.parse.urlparse(oauth_url)
        login_params = urllib.parse.parse_qs(parsed_login.query)
        redirect_to = login_params.get("redirect_to", [""])[0]
        if redirect_to:
            if redirect_to.startswith("/"):
                oauth_url = "https://discord.com" + redirect_to
            else:
                oauth_url = redirect_to
            print(f"   从 redirect_to 解码出 OAuth URL: {oauth_url[:120]}...")

    parsed = urllib.parse.urlparse(oauth_url)
    params = urllib.parse.parse_qs(parsed.query)

    client_id = params.get("client_id", [""])[0]
    redirect_uri = params.get("redirect_uri", [""])[0]
    scope = params.get("scope", ["identify email"])[0]
    state = params.get("state", [""])[0]
    response_type = params.get("response_type", ["code"])[0]
    access_type = params.get("access_type", [""])[0]
    prompt = params.get("prompt", [""])[0]

    print(f"   Client ID:    {client_id}")
    print(f"   Redirect URI: {redirect_uri}")
    print(f"   Scope:        {scope}")
    print(f"   State:        {state[:20]}..." if state else "   State:        (空)")
    print(f"   Access Type:  {access_type}")
    print(f"   Prompt:       {prompt}")

    if not client_id or not redirect_uri:
        print("   ❌ 无法解析关键 OAuth 参数 (client_id 或 redirect_uri)")
        save_screenshot(page, "login_failed_parse")
        return False

    print(f"\n📌 第5步：通过 Discord API 完成授权")

    api_params_dict = {
        "client_id": client_id,
        "response_type": response_type,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
    }
    if access_type:
        api_params_dict["access_type"] = access_type
    if prompt:
        api_params_dict["prompt"] = prompt

    api_params = urllib.parse.urlencode(api_params_dict)
    authorize_api = f"https://discord.com/api/v9/oauth2/authorize?{api_params}"

    referer_params_dict = dict(api_params_dict)
    referer_params = urllib.parse.urlencode(referer_params_dict)
    referer = f"https://discord.com/oauth2/authorize?{referer_params}"

    headers = {
        "accept": "*/*",
        "authorization": dc_token.strip(),
        "content-type": "application/json",
        "origin": "https://discord.com",
        "referer": referer,
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        "x-discord-locale": "zh-CN",
    }

    body = {
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    }

    try:
        resp = requests.post(authorize_api, headers=headers, json=body, timeout=20)
        print(f"   API 响应状态码: {resp.status_code}")

        if resp.status_code != 200:
            print(f"   ❌ Discord 授权失败: HTTP {resp.status_code}")
            print(f"   响应内容: {resp.text[:300]}")
            return False

        resp_data = resp.json()
    except Exception as e:
        print(f"   ❌ Discord API 请求异常: {e}")
        return False

    location = resp_data.get("location", "")
    if not location:
        print(f"   ❌ 授权响应中未找到 location 字段")
        print(f"   响应内容: {json.dumps(resp_data, ensure_ascii=False)[:300]}")
        return False

    masked_location = re.sub(r"code=[^&]+", "code=***", location)
    print(f"   ✅ 拿到回调 URL: {masked_location}")

    print(f"\n📌 第6步：通过回调 URL 完成登录写入 Cookie")
    try:
        page.goto(location, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"   ⚠️ 回调页面加载异常（可能正常）: {e}")

    ok, final_url = handle_turnstile_and_finish_login(page)
    print(f"   回调后最终 URL: {final_url}")

    if ok and final_url.startswith("https://openworld.eu.org"):
        print(f"   ✅ 登录成功！当前 URL: {final_url}")
        save_screenshot(page, "login_success")
        return True

    print(f"   ❌ 登录未完成，停留在: {final_url}（可能卡在 Turnstile 验证或回调）")
    save_screenshot(page, "login_callback_stuck")
    return False


def extract_gif_frames(gif_bytes: bytes) -> list:
    gif = Image.open(io.BytesIO(gif_bytes))
    frames = []
    try:
        while True:
            frame = gif.convert("L")
            frames.append(frame.copy())
            gif.seek(gif.tell() + 1)
    except EOFError:
        pass
    print(f"   📊 成功提取 GIF 共 {len(frames)} 帧")
    return frames


def preprocess_frame(img: Image.Image) -> Image.Image:
    threshold = 170
    binary = img.point(lambda p: 0 if p < threshold else 255, "L")
    w, h = binary.size
    binary = binary.resize((w * 2, h * 2), Image.LANCZOS)
    return binary


def recognize_captcha_by_frames(gif_bytes: bytes, ocr) -> str:
    from collections import Counter

    frames = extract_gif_frames(gif_bytes)
    if not frames:
        return ""

    left_candidates = []
    op_candidates = []
    right_candidates = []

    for idx, frame in enumerate(frames):
        w, h = frame.size
        left_crop = frame.crop((0, 0, int(w * 0.52), h))
        mid_crop = frame.crop((int(w * 0.30), 0, int(w * 0.70), h))
        right_crop = frame.crop((int(w * 0.50), 0, w, h))

        for region_name, crop_img, cand_list in [
            ("Left", left_crop, left_candidates),
            ("Middle", mid_crop, op_candidates),
            ("Right", right_crop, right_candidates)
        ]:
            proc_img = preprocess_frame(crop_img)
            img_buf = io.BytesIO()
            proc_img.save(img_buf, format="PNG")
            res = ocr.classification(img_buf.getvalue()).strip()

            if region_name in ("Left", "Right"):
                s = res.replace('I2', '12').replace('l1', '11')
                s = s.replace('t0', '10').replace('1o', '10').replace('1c', '10').replace('I0', '10')
                s = s.replace('ll', '11').replace('li', '11').replace('II', '11').replace('i1', '11')
                s = s.replace('O', '0').replace('o', '0').replace('c', '0').replace('d', '0')
                s = s.replace('l', '1').replace('I', '1').replace('t', '1').replace('T', '1')
                s = s.replace('S', '5').replace('s', '5')
                s = s.replace('Z', '2').replace('z', '2')
                s = s.replace('B', '8').replace('b', '6')
                s = s.replace('g', '9').replace('q', '9')
                s = s.replace('>', '7')
                res_clean = re.sub(r'[^0-9]', '', s)
            else:
                res_clean = ""
                for char in res:
                    if char in ("*", "x", "X", "×", "y"):
                        res_clean += "*"
                    elif char in ("+", "十", "t", "T", "┴", "⊥", "丄"):
                        res_clean += "+"
                    elif char in ("-", "—", "–", "一"):
                        res_clean += "-"
                    elif char in ("÷", ":"):
                        res_clean += "/"

            if res_clean:
                cand_list.append(res_clean)

    def pick_best_num(cand_list):
        if not cand_list:
            return ""
        two_digits = [c for c in cand_list if len(c) == 2]
        if two_digits:
            return Counter(two_digits).most_common(1)[0][0]
        return Counter(cand_list).most_common(1)[0][0]

    num_a = pick_best_num(left_candidates)
    num_b = pick_best_num(right_candidates)

    if "*" in op_candidates and op_candidates.count("*") >= 2:
        op = "*"
    elif "+" in op_candidates:
        op = "+"
    elif "*" in op_candidates:
        op = "*"
    elif "-" in op_candidates:
        op = "-"
    else:
        op = "-"

    print(f"   🔍 跨帧区域统计结果 -> 左数字(A): '{num_a}' | 运算符: '{op}' | 右数字(B): '{num_b}'")

    if num_a and num_b:
        expr = f"{num_a}{op}{num_b}"
        try:
            val = int(eval(expr))
            print(f"   🧮 算式求解成功: {expr} = {val}")
            return str(val)
        except Exception as e:
            print(f"   ⚠️ 计算异常 ({expr}): {e}")

    all_text = []
    for frame in frames:
        proc_img = preprocess_frame(frame)
        img_buf = io.BytesIO()
        proc_img.save(img_buf, format="PNG")
        res = ocr.classification(img_buf.getvalue()).strip()
        cleaned = re.sub(r'[^0-9+\-*/]', '', res.replace('x', '*').replace('X', '*').replace('O', '0').replace('o', '0').replace('l', '1'))
        if cleaned:
            all_text.append(cleaned)

    if all_text:
        most_common_full = Counter(all_text).most_common(1)[0][0]
        match = re.search(r'(\d+)\s*([+\-*/])\s*(\d+)', most_common_full)
        if match:
            a, o, b = match.groups()
            val = int(eval(f"{a}{o}{b}"))
            print(f"   🧮 全图统计求解: {a}{o}{b} = {val}")
            return str(val)

    return ""


def download_captcha_gif(page) -> bytes:
    """
    从页面中获取验证码图片的原始字节数据。
    兼容 <img> 和 <canvas>。
    """
    import base64

    captcha_selectors = [
        "img[alt='Captcha']",
        "img[alt='captcha']",
        "img[src*='captcha']",
        "img[src*='Captcha']",
        ".captcha img",
        "[class*='captcha'] img",
        "[id*='captcha'] img",
        "dialog img",
        "[role='dialog'] img",
        ".modal img",
        "img[src^='blob:']",
        "img[src^='data:image']",
    ]

    captcha_element = None
    matched_selector = None
    for selector in captcha_selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=3000):
                captcha_element = el
                matched_selector = selector
                print(f"   找到验证码元素 (选择器: {selector})")
                break
        except Exception:
            continue

    # 如果选择器都没找到，尝试找弹窗里所有可见 img
    if not captcha_element:
        print("   ⚠️ 常规选择器未找到，尝试遍历页面上所有可见 img...")
        try:
            imgs = page.locator("img").all()
            for i, img in enumerate(imgs):
                try:
                    if img.is_visible(timeout=500):
                        src = img.get_attribute("src") or ""
                        if src:
                            captcha_element = img
                            matched_selector = f"img[{i}]"
                            print(f"   🔎 使用第 {i} 个可见 img, src={src[:80]}")
                            break
                except Exception:
                    continue
        except Exception as e:
            print(f"   ⚠️ 遍历 img 失败: {e}")

    if not captcha_element:
        print("   ❌ 未找到任何可见的验证码图片")
        return None

    # 尝试获取 src
    src = ""
    try:
        src = captcha_element.get_attribute("src") or ""
    except Exception:
        pass

    print(f"   📥 验证码 src: {src[:100]}")

    # ========== 方法1：blob: URL ==========
    if src.startswith("blob:"):
        print("   📦 检测到 blob: URL，通过浏览器内 fetch 获取完整 GIF...")
        try:
            b64_data = page.evaluate("""
                async (blobUrl) => {
                    try {
                        const resp = await fetch(blobUrl);
                        const arrayBuffer = await resp.arrayBuffer();
                        const bytes = new Uint8Array(arrayBuffer);
                        let binary = '';
                        for (let i = 0; i < bytes.length; i++) {
                            binary += String.fromCharCode(bytes[i]);
                        }
                        return btoa(binary);
                    } catch (e) {
                        return null;
                    }
                }
            """, src)
            if b64_data:
                gif_bytes = base64.b64decode(b64_data)
                print(f"   ✅ 通过 blob fetch 获取成功 ({len(gif_bytes)} bytes)")
                return gif_bytes
            else:
                print("   ⚠️ blob fetch 返回空")
        except Exception as e:
            print(f"   ⚠️ blob fetch 失败: {e}")

    # ========== 方法2：http/https ==========
    elif src.startswith("http"):
        try:
            cookies = page.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            resp = requests.get(src, cookies=cookie_dict, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 100:
                print(f"   ✅ HTTP 下载成功 ({len(resp.content)} bytes)")
                return resp.content
            else:
                print(f"   ⚠️ HTTP 下载失败: {resp.status_code}, {len(resp.content)} bytes")
        except Exception as e:
            print(f"   ⚠️ HTTP 下载异常: {e}")

    # ========== 方法3：相对路径 ==========
    elif src.startswith("/"):
        full_url = f"{SITE_BASE}{src}"
        try:
            cookies = page.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            resp = requests.get(full_url, cookies=cookie_dict, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 100:
                print(f"   ✅ 相对路径下载成功 ({len(resp.content)} bytes)")
                return resp.content
        except Exception as e:
            print(f"   ⚠️ 相对路径下载异常: {e}")

    # ========== 方法4：data: URL ==========
    elif src.startswith("data:"):
        try:
            b64_part = src.split(",", 1)[1]
            gif_bytes = base64.b64decode(b64_part)
            print(f"   ✅ data: URL 解码成功 ({len(gif_bytes)} bytes)")
            return gif_bytes
        except Exception as e:
            print(f"   ⚠️ data: URL 解码失败: {e}")

    # ========== 方法5：元素截图 ==========
    print("   ⚠️ 所有下载方式失败，回退到元素截图（只能获取单帧）")
    try:
        screenshot_bytes = captcha_element.screenshot()
        print(f"   ✅ 元素截图成功 ({len(screenshot_bytes)} bytes)")
        return screenshot_bytes
    except Exception as e:
        print(f"   ❌ 截图也失败了: {e}")
        return None


def try_renew_captcha(page, initial_days: int, max_attempts=5) -> bool:
    try:
        import ddddocr
    except ImportError:
        print("   ⚠️ ddddocr 未安装，无法执行验证码识别")
        return False

    ocr = ddddocr.DdddOcr(show_ad=False)

    for attempt in range(1, max_attempts + 1):
        print(f"\n   {'='*40}")
        print(f"   🔄 第 {attempt}/{max_attempts} 次尝试")
        print(f"   {'='*40}")

        # ========== 重试前先刷新页面，恢复干净状态 ==========
        if attempt > 1:
            print("   🔄 刷新页面恢复干净状态...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                wait_for_cloudflare(page)
                time.sleep(3)
            except Exception as e:
                print(f"   ⚠️ 刷新异常: {e}")

        try:
            # ========== 第1步：点击 Renew free ==========
            print("   🔍 寻找并点击 [Renew free] 按钮...")
            renew_selectors = [
                "button:has-text('Renew free')",
                "button:has-text('Renew')",
                "a:has-text('Renew free')",
                "a:has-text('Renew')",
                "[class*='renew']",
            ]
            clicked = False
            for selector in renew_selectors:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=3000):
                        btn_text = btn.inner_text()
                        print(f"   找到按钮: '{btn_text}' (选择器: {selector})")
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                print("   ❌ 未找到可用的续期按钮")
                # 保存页面状态便于诊断
                dump_page_debug(page, f"renew_btn_not_found_{attempt}")
                continue

            # 等待弹窗和验证码加载
            print("   ⏳ 等待弹窗/验证码加载...")
            time.sleep(4)

            # 保存弹窗后的页面状态（第一次尝试时保存详细调试信息）
            if attempt == 1:
                dump_page_debug(page, "after_renew_click")

            # ========== 第2步：下载验证码 ==========
            gif_bytes = download_captcha_gif(page)
            if not gif_bytes:
                print("   ⚠️ 未获取到验证码图片")
                dump_page_debug(page, f"no_captcha_{attempt}")
                continue

            # 保存原始文件（调试用）
            gif_path = os.path.join(SCREENSHOT_DIR, f"captcha_raw_{attempt}.gif")
            try:
                with open(gif_path, "wb") as f:
                    f.write(gif_bytes)
                print(f"   💾 验证码原始文件已保存: {gif_path}")
            except Exception:
                pass

            # ========== 第3步：识别 ==========
            answer = recognize_captcha_by_frames(gif_bytes, ocr)
            if not answer:
                print("   ⚠️ 验证码识别求解失败")
                continue

            print(f"   📝 最终计算答案: {answer}")

            # ========== 第4步：填入并提交 ==========
            input_selectors = [
                "input[placeholder='Answer']",
                "input[placeholder='answer']",
                "input[name='captcha']",
                "input[name='answer']",
                "input[type='text']",
            ]

            input_filled = False
            for selector in input_selectors:
                try:
                    inp = page.locator(selector).first
                    if inp.is_visible(timeout=3000):
                        inp.fill("")
                        inp.fill(answer)
                        input_filled = True
                        print(f"   ✅ 答案已填入: {answer} (选择器: {selector})")
                        break
                except Exception:
                    continue

            if not input_filled:
                print("   ❌ 未找到验证码输入框")
                dump_page_debug(page, f"no_input_{attempt}")
                continue

            confirm_selectors = [
                "button:has-text('Confirm Renewal')",
                "button:has-text('Confirm')",
                "button:has-text('Submit')",
                "button[type='submit']",
            ]

            submitted = False
            for selector in confirm_selectors:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        submitted = True
                        print(f"   ✅ 已点击提交按钮 (选择器: {selector})")
                        break
                except Exception:
                    continue

            if not submitted:
                print("   ❌ 未找到提交按钮")
                continue

            # ========== 第5步：验证结果 ==========
            print("   ⏳ 等待提交请求处理完成...")
            time.sleep(4)

            print("   🔄 刷新页面验证最新剩余天数...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"   ⚠️ 页面刷新异常: {e}")
            wait_for_cloudflare(page)
            time.sleep(2)

            page_text = page.locator("body").inner_text()
            match = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", page_text)

            if match:
                new_days = int(match.group(1))
                print(f"   📊 刷新后最新剩余天数: {new_days} 天")

                if new_days >= 6:
                    print(f"   ✅ 续期成功！天数已从 {initial_days} 天更新为 {new_days} 天")
                    return True
                else:
                    print(f"   ❌ 续期失败！天数仍为 {new_days} 天（未达到 6 天）")
                    continue
            else:
                print("   ⚠️ 页面刷新后无法解析剩余天数")
                continue

        except Exception as e:
            print(f"   ❌ 第 {attempt} 次尝试发生错误: {e}")
            import traceback
            traceback.print_exc()
            continue

    print(f"   ❌ {max_attempts} 次尝试均失败")
    return False


def get_vps_urls(page) -> list:
    vps_urls = []

    def extract_vps_links():
        found = []
        try:
            links = page.locator("a[href*='/vps/']").all()
            for link in links:
                href = link.get_attribute("href") or ""
                if href:
                    full_url = urllib.parse.urljoin(SITE_BASE, href)
                    path = urllib.parse.urlparse(full_url).path.rstrip('/')
                    if path != "/vps" and full_url not in found:
                        found.append(full_url)
        except Exception as e:
            print(f"   ⚠️ 提取 VPS 链接异常: {e}")
        return found

    print("\n🔍 正在自动识别账号下的 VPS 实例...")
    vps_urls = extract_vps_links()

    if not vps_urls:
        try:
            print(f"   前往首页 {SITE_BASE} 提取实例列表...")
            page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
            wait_for_cloudflare(page)
            time.sleep(3)
            vps_urls = extract_vps_links()
        except Exception as e:
            print(f"   ⚠️ 前往首页提取失败: {e}")

    if not vps_urls:
        for sub_path in ["/dashboard", "/vps"]:
            try:
                url = f"{SITE_BASE}{sub_path}"
                print(f"   尝试访问 {url} 提取实例列表...")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                wait_for_cloudflare(page)
                time.sleep(3)
                vps_urls = extract_vps_links()
                if vps_urls:
                    break
            except Exception:
                pass

    if vps_urls:
        print(f"   ✅ 成功检测到 {len(vps_urls)} 个 VPS 实例:")
        for u in vps_urls:
            print(f"      - {u}")
    else:
        print("   ❌ 未能在控制面板自动检测到任何 VPS 实例页面")

    return vps_urls


def main():
    print("#" * 50)
    print("   Openworld VPS 自动续期脚本")
    print("#" * 50)

    if not DISCORD_TOKEN:
        print("❌ 未找到 DISCORD_TOKEN 环境变量，请检查配置。")
        sys.exit(1)

    headless_mode = os.environ.get("HEADLESS", "true").lower() == "true"
    print(f"🖥️  运行模式: {'无头' if headless_mode else '有头'}")
    print("🎯 登录后将自动从面板检测 VPS 实例")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless_mode,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 720},
        )
        # 反自动化检测：抹掉 Playwright 指纹，帮助通过 Cloudflare Turnstile
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = window.chrome || { runtime: {}, loadTimes: function(){}, csi: function(){} };
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            const _gpo = Object.getOwnPropertyDescriptor(Navigator.prototype, 'userAgent');
            Object.defineProperty(navigator, 'userAgent', {get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'});
            window.Notification = undefined;
        """)
        page = context.new_page()

        try:
            success = login_with_discord_token(page, DISCORD_TOKEN)

            if not success:
                print("\n❌ 登录流程失败，脚本退出。")
                send_telegram_message("❌ Openworld VPS 续期失败：登录流程失败")
                # 失败必须非零退出，否则 GitHub Actions 会误报成功
                sys.exit(1)

            target_vps_list = get_vps_urls(page)

            if not target_vps_list:
                print("\n❌ 未能从面板自动检测到任何 VPS 实例。")
                save_screenshot(page, "no_vps_found")
                send_telegram_message("❌ Openworld VPS 续期失败：未在面板找到任何 VPS 实例")
                # 失败必须非零退出，否则 GitHub Actions 会误报成功
                sys.exit(1)

            # 跟踪是否有实例需要续期但失败（任一失败则整体非零退出，避免误报成功）
            renew_failed_any = False

            for idx, target_url in enumerate(target_vps_list, 1):
                print(f"\n{'=' * 50}")
                print(f"📌 [{idx}/{len(target_vps_list)}] 导航到目标 VPS 页面: {target_url}")
                print(f"{'=' * 50}")

                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"⚠️ 页面加载异常: {e}")

                wait_for_cloudflare(page)
                time.sleep(3)

                current_url = page.url
                page_title = page.title()
                print(f"📝 当前 URL: {current_url}")
                print(f"📝 页面标题: {page_title}")

                if "/login" in current_url:
                    print("❌ 被重定向到登录页，Cookie 可能无效")
                    save_screenshot(page, f"redirect_to_login_{idx}")
                    send_telegram_message("❌ Openworld VPS 续期失败：登录后仍被重定向到登录页")
                    break

                page_text = page.locator("body").inner_text()

                if "404" in page_title or "Page Not Found" in page_title or "doesn't exist" in page_text.lower():
                    print(f"❌ 目标 VPS 页面不存在或无权访问 (404 Not Found): {target_url}")
                    save_screenshot(page, f"vps_404_{idx}")
                    send_telegram_message(f"❌ Openworld VPS 续期失败：页面 404 Not Found\nURL: {target_url}")
                    continue

                print("✅ 已成功到达目标 VPS 页面")
                save_screenshot(page, f"vps_page_loaded_{idx}")

                match = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", page_text)

                if match:
                    days_left = int(match.group(1))
                    print(f"🔍 当前 VPS 剩余续期时间: {days_left} 天")

                    if days_left > RENEW_THRESHOLD_DAYS:
                        msg = f"⏳ 剩余 {days_left} 天 > {RENEW_THRESHOLD_DAYS} 天阈值，跳过续期"
                        print(msg)
                        send_telegram_message(f"ℹ️ Openworld VPS 无需续期\n实例: {target_url}\n剩余时间: {days_left} 天")
                        continue
                    else:
                        print(f"⚠️ 剩余 {days_left} 天 ≤ {RENEW_THRESHOLD_DAYS} 天，开始执行续期...")
                else:
                    print("⚠️ 未能从页面提取剩余天数，将强制尝试续期")
                    print(f"   页面文本片段: {page_text[:500]}")
                    days_left = 0

                print(f"\n{'=' * 50}")
                print("🔄 开始执行验证码续期")
                print(f"{'=' * 50}")

                renew_success = try_renew_captcha(page, initial_days=days_left)

                if renew_success:
                    expiry_time = datetime.now(timezone(timedelta(hours=8))) + timedelta(days=6)
                    expiry_str = expiry_time.strftime("%Y-%m-%d %H:%M:%S") + " (GMT+8)"
                    msg = f"✅ Openworld VPS 续期成功！\n实例: {target_url}\n天数已更新为 6 天\n续期至: {expiry_str}"
                    print(f"✅ 续期成功！天数已更新为 6 天")
                    print(f"📅 续期至: {expiry_str}")
                    send_telegram_message(msg)
                else:
                    print("❌ 续期失败（5次尝试均未成功）")
                    send_telegram_message(f"❌ Openworld VPS 续期失败：5次验证码尝试均未成功\n实例: {target_url}")
                    renew_failed_any = True

            # 有实例续期失败 → 非零退出，让 GitHub Actions 标记失败并发送失败通知
            if renew_failed_any:
                sys.exit(1)

        except Exception as e:
            print(f"\n💥 脚本发生未捕获异常: {e}")
            import traceback
            traceback.print_exc()
            save_screenshot(page, "uncaught_error")
            send_telegram_message(f"❌ Openworld VPS 续期脚本异常: {str(e)[:200]}")
            # 异常必须非零退出，避免误报成功
            sys.exit(1)

        finally:
            browser.close()
            print("\n🏁 脚本执行完毕")


if __name__ == "__main__":
    main()
