import io
import json
import os
import re
import time
import base64
from typing import List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract
import paho.mqtt.client as mqtt

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise RuntimeError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


CONFIG = load_config(CONFIG_PATH)

LOGIN_PAGE_URL = CONFIG.get(
    "TAIPOWER_LOGIN_URL",
    "https://service.taipower.com.tw/hvcs/",
)
LOGIN_POST_URL = CONFIG.get(
    "TAIPOWER_LOGIN_POST_URL",
    "https://service.taipower.com.tw/hvcs/SignOn/Login",
)
CAPTCHA_URL = CONFIG.get(
    "TAIPOWER_CAPTCHA_URL",
    "https://service.taipower.com.tw/hvcs/Other/Module/Chptcha",
)
DASHBOARD_URL = CONFIG.get(
    "TAIPOWER_DASHBOARD_URL",
    "https://service.taipower.com.tw/hvcs/Electricity/Module/DashBoard",
)
UID_METER_URL = CONFIG.get(
    "TAIPOWER_UID_METER_URL",
    "https://service.taipower.com.tw/hvcs/Customer/Module/UIDMeterNoList",
)
METER_LIST_URL = CONFIG.get(
    "TAIPOWER_METER_LIST_URL",
    "https://service.taipower.com.tw/hvcs/Controllers/Members/UserInfo/MeterNoList",
)

# 是否啟用 2captcha
USE_2CAPTCHA = bool(CONFIG.get("USE_2CAPTCHA", False))

# 你的 2captcha API KEY
CAPTCHA_2CAPTCHA_API_KEY = CONFIG.get("CAPTCHA_2CAPTCHA_API_KEY", "")
USERNAME = CONFIG.get("TAIPOWER_USERNAME", "")
PASSWORD = CONFIG.get("TAIPOWER_PASSWORD", "")
METER_NO = CONFIG.get("TAIPOWER_METER_NO", "")
MAX_LOGIN_ATTEMPTS = int(CONFIG.get("TAIPOWER_MAX_ATTEMPTS", 6))

MQTT_HOST = CONFIG.get("MQTT_HOST", "jetsion.com")
MQTT_PORT = int(CONFIG.get("MQTT_PORT", 1883))
MQTT_TOPIC = CONFIG.get("MQTT_TOPIC", "taipower/hvcs")
MQTT_USERNAME = CONFIG.get("MQTT_USERNAME", "")
MQTT_PASSWORD = CONFIG.get("MQTT_PASSWORD", "")

TESSERACT_CMD = CONFIG.get("TESSERACT_CMD", "")
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

TESSERACT_CONFIG = "--psm 8 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
TESSERACT_ALT_CONFIG = "--psm 7 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
CAPTCHA_DEBUG = bool(CONFIG.get("CAPTCHA_DEBUG", False))
CAPTCHA_MANUAL = bool(CONFIG.get("CAPTCHA_MANUAL", False))
SAVE_LOGIN_HTML = bool(CONFIG.get("SAVE_LOGIN_HTML", False))
SAVE_LOGIN_RESPONSE = bool(CONFIG.get("SAVE_LOGIN_RESPONSE", False))
DASHBOARD_DEBUG = bool(CONFIG.get("DASHBOARD_DEBUG", False))
METER_DEBUG = bool(CONFIG.get("METER_DEBUG", False))


class LoginError(RuntimeError):
    pass


def get_login_page(session: requests.Session) -> str:
    urls = [
        LOGIN_PAGE_URL,
        "https://service.taipower.com.tw/hvcs/SignOn/Module/Login",
    ]
    last_err = None
    for url in urls:
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            if "__RequestVerificationToken" in resp.text:
                return resp.text
        except requests.RequestException as exc:
            last_err = exc
            continue

    if last_err:
        raise last_err
    raise LoginError("Unable to load login page")


def parse_login_form(html: str) -> tuple[str, dict, str]:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", {"id": "loginForm"})
    if not form:
        raise LoginError("Login form not found")

    action = form.get("action", "").strip()
    if not action:
        raise LoginError("Login form action not found")

    fields = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        fields[name] = inp.get("value", "")

    captcha_img = soup.select_one("#captchaImage")
    captcha_src = ""
    if captcha_img:
        captcha_src = captcha_img.get("src", "").strip()
    if not captcha_src:
        captcha_src = "/hvcs/Other/Module/Chptcha"

    action_url = requests.compat.urljoin(LOGIN_PAGE_URL, action)
    captcha_url = requests.compat.urljoin(LOGIN_PAGE_URL, captcha_src)
    return action_url, fields, captcha_url


def extract_token(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    token_input = soup.find("input", {"name": "__RequestVerificationToken"})
    if not token_input or not token_input.get("value"):
        raise LoginError("Missing __RequestVerificationToken on login page")
    return token_input["value"]


def fetch_captcha(session: requests.Session, url: str) -> Image.Image:
    resp = session.get(url, headers={"Referer": LOGIN_PAGE_URL}, timeout=20)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content))


def _clean_text(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]", "", text)
    return cleaned


def _variants(img: Image.Image) -> list[Image.Image]:
    gray = img.convert("L")
    variants = []
    scales = (2, 3, 4)
    thresholds = (60, 75, 90, 105, 120, 135, 150, 165, 180, 195)
    contrasts = (1.2, 1.5, 1.8)

    for scale in scales:
        resized = gray.resize((gray.width * scale, gray.height * scale), Image.BICUBIC)
        base = ImageOps.autocontrast(resized)
        variants.append(base)
        variants.append(base.filter(ImageFilter.SHARPEN))
        variants.append(base.filter(ImageFilter.UnsharpMask(radius=1, percent=150, threshold=3)))
        variants.append(base.filter(ImageFilter.MedianFilter(3)))
        variants.append(base.filter(ImageFilter.MinFilter(3)))
        variants.append(base.filter(ImageFilter.MaxFilter(3)))

        for contrast in contrasts:
            enhanced = ImageEnhance.Contrast(base).enhance(contrast)
            variants.append(enhanced)
            for thresh in thresholds:
                bw = enhanced.point(lambda x, t=thresh: 255 if x > t else 0, mode="1")
                variants.append(bw)
                variants.append(ImageOps.invert(bw.convert("L")))

    return variants

def solve_captcha_2captcha(img: Image.Image, timeout: int = 120) -> str:
    """
    使用 2Captcha 解圖形驗證碼
    """
    if not CAPTCHA_2CAPTCHA_API_KEY:
        raise LoginError("2Captcha API key not set")

    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    img_b64 = base64.b64encode(buffered.getvalue()).decode()

    # Step 1: upload captcha
    resp = requests.post(
        "http://2captcha.com/in.php",
        data={
            "key": CAPTCHA_2CAPTCHA_API_KEY,
            "method": "base64",
            "body": img_b64,
            "json": 1,
        },
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()

    if result.get("status") != 1:
        raise LoginError(f"2Captcha upload failed: {result}")

    captcha_id = result["request"]

    # Step 2: poll result
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(5)
        poll = requests.get(
            "http://2captcha.com/res.php",
            params={
                "key": CAPTCHA_2CAPTCHA_API_KEY,
                "action": "get",
                "id": captcha_id,
                "json": 1,
            },
            timeout=30,
        )
        poll.raise_for_status()
        data = poll.json()

        if data.get("status") == 1:
            code = _clean_text(data.get("request", ""))
            if len(code) == 4:
                return code
            return ""

        if data.get("request") != "CAPCHA_NOT_READY":
            raise LoginError(f"2Captcha error: {data}")

    raise LoginError("2Captcha timeout")

def ocr_captcha(img: Image.Image) -> str:
    configs = (
        TESSERACT_CONFIG,
        TESSERACT_ALT_CONFIG,
        "--psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "--psm 10 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    )
    for variant in _variants(img):
        for config in configs:
            text = pytesseract.image_to_string(variant, config=config)
            cleaned = _clean_text(text)
            if len(cleaned) == 4:
                return cleaned
    return ""


def login_and_get_dashboard(session: requests.Session) -> str:
    if not USERNAME or not PASSWORD:
        raise LoginError("TAIPOWER_USERNAME or TAIPOWER_PASSWORD not set")

    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        login_ok = False
        print(f"Attempt {attempt}: loading login page...")
        html = get_login_page(session)
        if SAVE_LOGIN_HTML:
            with open(f"login_page_{attempt}.html", "w", encoding="utf-8") as fh:
                fh.write(html)
        action_url, fields, captcha_url = parse_login_form(html)
        token = extract_token(html)
        print(f"Attempt {attempt}: fetching captcha...")
        captcha_img = fetch_captcha(session, captcha_url)

        if USE_2CAPTCHA:
            try:
                print(f"Attempt {attempt}: solving captcha via 2captcha...")
                captcha_code = solve_captcha_2captcha(captcha_img)
            except Exception:
                captcha_code = ocr_captcha(captcha_img)
        else:
            print(f"Attempt {attempt}: solving captcha via OCR...")
            captcha_code = ocr_captcha(captcha_img)

            if CAPTCHA_MANUAL:
                try:
                    manual = input("Enter captcha (4 chars): ").strip()
                    if len(manual) == 4:
                        captcha_code = manual
                    else:
                        continue
                except EOFError:
                    continue
            else:
                if len(captcha_code) != 4:
                    print(f"Attempt {attempt}: OCR failed, retrying...")
                continue

        payload = dict(fields)
        payload.update(
            {
                "__RequestVerificationToken": token,
                "UserName": USERNAME,
                "UserPwd": PASSWORD,
                "Captcha": captcha_code,
            }
        )
        resp = session.post(
            action_url,
            data=payload,
            headers={
                "Referer": LOGIN_PAGE_URL,
                "RequestVerificationToken": token,
            },
            timeout=20,
        )
        if resp.status_code >= 400:
            print(f"Attempt {attempt}: login post failed {resp.status_code}")
        if SAVE_LOGIN_RESPONSE:
            with open(f"login_response_{attempt}.html", "w", encoding="utf-8") as fh:
                fh.write(resp.text)
            print(f"Attempt {attempt}: saved login_response_{attempt}.html")
        redirect_url = None
        if resp.headers.get("Content-Type", "").lower().startswith("application/json"):
            try:
                data = resp.json()
            except ValueError:
                data = {}
            data_block = data.get("data") if isinstance(data.get("data"), dict) else {}
            status = data_block.get("Status")
            message = data_block.get("Message")
            refresh = data_block.get("refreshChptcha")
            redirect_url = data_block.get("Url")
            if status is False:
                if refresh:
                    print(f"Attempt {attempt}: server indicates captcha error")
                if message:
                    print(f"Attempt {attempt}: server message: {message}")
            if status and redirect_url:
                login_ok = True
                session.get(
                    requests.compat.urljoin(LOGIN_PAGE_URL, redirect_url),
                    timeout=20,
                )
            elif status:
                login_ok = True
        else:
            login_ok = resp.status_code < 400

        if login_ok and METER_NO:
            try:
                select_meter_no(session, METER_NO)
            except LoginError as exc:
                print(f"Attempt {attempt}: {exc}")
                time.sleep(1)
                continue
        print(f"Attempt {attempt}: fetching dashboard...")
        dash = session.get(DASHBOARD_URL, timeout=20)
        if DASHBOARD_DEBUG:
            with open(f"dashboard_{attempt}.html", "w", encoding="utf-8") as fh:
                fh.write(dash.text)
        if METER_DEBUG:
            dump_meter_debug(session)
        if "card_1" in dash.text:
            return dash.text

        err_text = re.sub(r"\s+", " ", resp.text)
        if "captcha" in err_text.lower():
            print(f"Attempt {attempt}: server indicates captcha error")
        print(f"Attempt {attempt}: login not successful")
        time.sleep(1)

    raise LoginError("Login failed after multiple attempts")


def _parse_number(value: str):
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return float(value)


def _parse_js_number(html: str, name: str):
    pattern = rf"(?:let|var)\s+{re.escape(name)}\s*=\s*(-?\d+(?:\.\d+)?)"
    match = re.search(pattern, html)
    if not match:
        return None
    return _parse_number(match.group(1))


def _parse_js_array(text: str) -> list:
    values = re.findall(r"-?\d+(?:\.\d+)?", text)
    return [_parse_number(value) for value in values]


def _format_value(value) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def _extract_union_chart(html: str) -> Tuple[List, List]:
    idx = html.find("UnionChart")
    if idx == -1:
        return [], []
    section = html[idx : idx + 12000]
    categories = []
    match = re.search(r"categories:\s*\[([^\]]*)\]", section)
    if match:
        categories = _parse_js_array(match.group(1))

    series = []
    for match in re.finditer(
        r"name:\s*'([^']+)'[\s\S]*?data:\s*\[([^\]]*)\]",
        section,
    ):
        name = match.group(1).strip()
        values = _parse_js_array(match.group(2))
        if values:
            series.append((name, values))
    return categories, series


def _extract_chart_name(html: str, chart_id: str) -> str:
    pattern = rf"Highcharts\.chart\('{re.escape(chart_id)}'[\s\S]*?name:\s*'([^']+)'"
    match = re.search(pattern, html)
    return match.group(1).strip() if match else ""


def parse_dashboard(html: str) -> Tuple[dict, List[Tuple[str, str]]]:
    soup = BeautifulSoup(html, "html.parser")

    def read_text(selector: str) -> str:
        node = soup.select_one(selector)
        return node.get_text(strip=True) if node else ""

    data = {}
    pairs: List[Tuple[str, str]] = []

    def add_field(key: str, value, label: Optional[str] = None) -> None:
        if value is None or value == "":
            return
        data[key] = value
        pairs.append((label or key, _format_value(value)))

    daily_title = read_text(".cardDH .DH_Title")
    monthly_title = read_text(".cardMH .DH_Title")
    daily_time = read_text(".cardDH .DH_Time")
    daily_kw = read_text(".cardDH .DH_kW")
    monthly_time = read_text(".cardMH .DH_Time")
    monthly_kw = read_text(".cardMH .DH_kW")

    add_field("daily_max_title", daily_title)
    add_field("daily_max_time", daily_time, label=daily_title or "當日最高需量時間")
    add_field(
        "daily_max_kw",
        daily_kw,
        label=f"{daily_title}_kw" if daily_title else "當日最高需量時間_kw",
    )
    add_field("monthly_max_title", monthly_title)
    add_field(
        "monthly_max_time",
        monthly_time,
        label=monthly_title or "當月最高需量時間",
    )
    add_field(
        "monthly_max_kw",
        monthly_kw,
        label=f"{monthly_title}_kw" if monthly_title else "當月最高需量時間_kw",
    )

    add_field("fetched_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    return data, pairs


def dump_meter_debug(session: requests.Session) -> None:
    pages = [
        ("meter_list.html", UID_METER_URL),
    ]
    for filename, url in pages:
        resp = session.get(url, timeout=20)
        if resp.status_code == 200:
            with open(filename, "w", encoding="utf-8") as fh:
                fh.write(resp.text)

    endpoints = {
        "role_subgroup.json": "/hvcs/Controllers/Manager/getRoleSubGroup/getRoleSubGroup",
        "meter_subgroup.json": "/hvcs/Controllers/Manager/getRoleSubGroup/getMeterRoleSubGroup",
        "meter_competence.json": "/hvcs/Controllers/Manager/getRoleSubGroup/getMeterCompetence",
    }
    for filename, path in endpoints.items():
        url = requests.compat.urljoin(LOGIN_PAGE_URL, path)
        resp = session.get(url, timeout=20)
        if resp.status_code == 200:
            with open(filename, "w", encoding="utf-8") as fh:
                fh.write(resp.text)


def select_meter_no(session: requests.Session, meter_no: str) -> None:
    if not meter_no:
        return
    headers = {
        "Referer": UID_METER_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json; charset=utf-8",
    }
    resp = session.post(METER_LIST_URL, json={}, headers=headers, timeout=20)
    if resp.status_code >= 400:
        raise LoginError(f"Meter list request failed {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        if METER_DEBUG:
            with open("meter_list_response.html", "w", encoding="utf-8") as fh:
                fh.write(resp.text)
        try:
            cleaned = re.sub(r"new Date\(\s*([-\d]+)\s*\)", r"\1", resp.text)
            payload = json.loads(cleaned)
        except ValueError as inner_exc:
            raise LoginError("Meter list response is not JSON") from inner_exc
    data = payload.get("data") if isinstance(payload.get("data"), list) else []
    meter_list = []
    for item in data:
        grouped = item.get("UserMeter_GroupbyMeter") if isinstance(item, dict) else None
        if grouped and grouped.get("MeterNo"):
            meter_list.append(str(grouped["MeterNo"]))
    if meter_no not in meter_list:
        raise LoginError(f"MeterNo {meter_no} not found in account list")
    session.post(
        UID_METER_URL,
        data={"MeterNo": meter_no},
        headers={"Referer": UID_METER_URL},
        timeout=20,
    )
    session.get(
        UID_METER_URL,
        params={"MeterNo": meter_no},
        headers={"Referer": UID_METER_URL},
        timeout=20,
    )


def format_mqtt_message(pairs: List[Tuple[str, str]]) -> str:
    return "@".join(f"{name}:{value}" for name, value in pairs)


def publish_mqtt(message: str) -> None:
    client = mqtt.Client()
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.publish(MQTT_TOPIC, message, retain=True)
    client.disconnect()


def main() -> None:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        }
    )
    dashboard_html = login_and_get_dashboard(session)
    payload, pairs = parse_dashboard(dashboard_html)
    message = format_mqtt_message(pairs)
    publish_mqtt(message)
    print("Published payload:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("Published MQTT message:")
    print(message)


if __name__ == "__main__":
    main()
