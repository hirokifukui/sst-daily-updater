#!/usr/bin/env python3
"""
SST (海水面温度) 日次自動更新スクリプト

NASA MUR SST データを取得し、Supabase sst_daily テーブルにアップロード。

使い方:
  python sync.py              # 日次更新（差分取得）
  python sync.py --full       # 全期間一括（2002〜）
  python sync.py --dry-run    # 確認のみ
  python sync.py --site sesoko  # 特定サイトのみ
"""

import argparse
import os
import sys
import time
import json
import ssl
import urllib.request
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

# =============================================================================
# 設定
# =============================================================================

SCRIPT_DIR = Path(__file__).parent
ENV_PATH = SCRIPT_DIR / ".env"
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "sync_state.json"
LOG_PATH = SCRIPT_DIR / "sync.log"

def load_dotenv():
    """シンプルな.envファイル読み込み"""
    if ENV_PATH.exists():
        with open(ENV_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

load_dotenv()

# 環境変数
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# NASA MUR SST ERDDAP
ERDDAP_URL = "https://coastwatch.pfeg.noaa.gov/erddap/griddap/jplMURSST41.json"

# SSLコンテキスト（証明書検証スキップ）
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

# NASAデータの遅延日数（2-3日）
DATA_DELAY_DAYS = 3

# 取得対象サイト（7地点）
SITES = [
    {"code": "sesoko", "name": "瀬底", "lat": 26.6494, "lon": 127.8536, "mmm": 29.0},
    {"code": "manza", "name": "万座", "lat": 26.5080, "lon": 127.8540, "mmm": 29.0},
    {"code": "kerama", "name": "慶良間", "lat": 26.186, "lon": 127.374, "mmm": 29.0},
    {"code": "sekisei", "name": "石西礁湖", "lat": 24.337, "lon": 124.035, "mmm": 29.5},
    {"code": "amami", "name": "奄美", "lat": 28.105, "lon": 129.160, "mmm": 29.0},
    {"code": "ogasawara", "name": "小笠原", "lat": 27.0942, "lon": 142.1919, "mmm": 28.5},
    {"code": "kushimoto", "name": "串本", "lat": 33.470, "lon": 135.696, "mmm": 28.0},
]

# 全期間取得時の開始日
FULL_START_DATE = "2002-06-01"

UTC = timezone.utc
JST = timezone(timedelta(hours=9))

RETRY_COUNT = 3
RETRY_DELAY = 10
TIMEOUT = 120

# =============================================================================
# ロギング
# =============================================================================

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_PATH, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# =============================================================================
# ステート管理
# =============================================================================

def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"last_sync": None, "sites": {}}

def save_state(state: dict):
    state["last_sync"] = datetime.now().isoformat()
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

# =============================================================================
# ユーティリティ
# =============================================================================

def check_env():
    """必須環境変数をチェック"""
    missing = []
    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not SUPABASE_KEY:
        missing.append("SUPABASE_KEY")
    
    if missing:
        logger.error(f"必須環境変数が未設定: {', '.join(missing)}")
        sys.exit(1)


def send_telegram(message, is_error=False):
    """Telegram通知を送信"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram設定なし（通知スキップ）")
        return False
    
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram送信失敗: {e}")
        return False


def supabase_get(endpoint, params=None):
    """Supabase REST API GET"""
    import requests
    
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json() if resp.text else None


def supabase_upsert(endpoint, data, batch_size=100):
    """Supabase REST API UPSERT"""
    import requests
    
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"
    }
    
    uploaded = 0
    for i in range(0, len(data), batch_size):
        batch = data[i:i+batch_size]
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/{endpoint}",
            headers=headers,
            json=batch,
            timeout=60
        )
        if resp.status_code in (200, 201):
            uploaded += len(batch)
        else:
            logger.warning(f"Upsert error: {resp.status_code} {resp.text[:200]}")
    
    return uploaded


def get_latest_date(site_code):
    """サイトの最新データ日付を取得"""
    try:
        result = supabase_get(
            "sst_daily",
            params={
                "site_code": f"eq.{site_code}",
                "select": "date",
                "order": "date.desc",
                "limit": "1"
            }
        )
        if result:
            return datetime.strptime(result[0]["date"], "%Y-%m-%d").date()
    except Exception as e:
        logger.warning(f"最新日付取得エラー ({site_code}): {e}")
    return None


def get_latest_sst(site_code):
    """サイトの最新SSTを取得"""
    try:
        result = supabase_get(
            "sst_daily",
            params={
                "site_code": f"eq.{site_code}",
                "select": "date,sst",
                "order": "date.desc",
                "limit": "1"
            }
        )
        if result:
            return result[0]
    except Exception as e:
        logger.warning(f"最新SST取得エラー ({site_code}): {e}")
    return None

# =============================================================================
# NASA ERDDAP取得
# =============================================================================

def fetch_sst_period(start_date: str, end_date: str, lat: float, lon: float) -> list[dict]:
    """NASA ERDDAPから指定期間のSSTデータを取得"""
    
    lat_min = lat - 0.01
    lat_max = lat + 0.01
    lon_min = lon - 0.01
    lon_max = lon + 0.01
    
    url = (
        f"{ERDDAP_URL}?"
        f"analysed_sst[({start_date}T09:00:00Z):1:({end_date}T09:00:00Z)]"
        f"[({lat_min}):1:({lat_max})]"
        f"[({lon_min}):1:({lon_max})]"
    )
    
    for attempt in range(RETRY_COUNT):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CONTEXT) as response:
                data = json.loads(response.read().decode())
            
            units = data.get("table", {}).get("columnUnits", [])
            sst_unit = units[3] if len(units) > 3 else "unknown"
            
            rows = data.get("table", {}).get("rows", [])
            results = []
            
            for row in rows:
                time_str, lat_val, lon_val, sst_value = row
                
                if sst_value is not None:
                    # ケルビン→摂氏変換
                    if sst_unit == "K" or sst_value > 100:
                        sst_celsius = sst_value - 273.15
                    else:
                        sst_celsius = sst_value
                    
                    # 異常値フィルタ
                    if 15.0 <= sst_celsius <= 35.0:
                        date_str = time_str[:10]
                        results.append({
                            "date": date_str,
                            "sst": round(sst_celsius, 2)
                        })
            
            # 同一日付を平均化
            daily_data = {}
            for r in results:
                d = r["date"]
                if d not in daily_data:
                    daily_data[d] = []
                daily_data[d].append(r["sst"])
            
            averaged = []
            for d, ssts in sorted(daily_data.items()):
                avg_sst = round(sum(ssts) / len(ssts), 2)
                averaged.append({"date": d, "sst": avg_sst})
            
            return averaged
            
        except Exception as e:
            if attempt < RETRY_COUNT - 1:
                logger.warning(f"Retry {attempt+1}: {e}")
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"Fetch failed: {e}")
                raise
    
    return []

# =============================================================================
# 通知
# =============================================================================

def get_temp_emoji(sst):
    """水温を絵文字で表現"""
    if sst is None:
        return "❓"
    if sst >= 29:
        return "🔥"
    elif sst >= 26:
        return "😊"
    elif sst >= 23:
        return "🌊"
    else:
        return "🥶"


def build_telegram_message(results, total_count, elapsed, errors):
    """Telegram通知メッセージを生成"""
    now_jst = datetime.now(JST)
    
    lines = [
        f"🌡️ SST日次更新 ({now_jst.strftime('%m/%d %H:%M JST')})",
        "━━━━━━━━━━━━━━━"
    ]
    
    # 各サイトの最新SST
    for site in SITES:
        latest = get_latest_sst(site["code"])
        if latest:
            sst = latest["sst"]
            emoji = get_temp_emoji(sst)
            lines.append(f"{emoji} {site['name']}: {sst:.1f}℃ ({latest['date']})")
        else:
            lines.append(f"❓ {site['name']}: データなし")
    
    lines.append("━━━━━━━━━━━━━━━")
    
    # 結果サマリ
    if errors:
        lines.append(f"⚠️ {total_count}件更新 (エラーあり)")
    elif total_count == 0:
        lines.append("✅ 更新なし（最新）")
    else:
        lines.append(f"✅ {total_count}件更新 ({elapsed:.0f}秒)")
    
    lines.append("🔥29℃↑ 😊26-28℃ 🌊23-25℃ 🥶22℃↓")
    
    return "\n".join(lines)

# =============================================================================
# メイン処理
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='NASA MUR SST 日次更新')
    parser.add_argument('--site', type=str, help='特定サイトのみ (sesoko/manza/ogasawara等)')
    parser.add_argument('--full', action='store_true', help='全期間一括（2002〜）')
    parser.add_argument('--dry-run', action='store_true', help='確認のみ')
    parser.add_argument('--force', action='store_true', help='強制再取得')
    args = parser.parse_args()
    
    check_env()
    
    logger.info("=" * 60)
    logger.info("SST日次更新 開始")
    if args.dry_run:
        logger.info("DRY-RUN MODE")
    
    start_time = time.time()
    now = datetime.now(UTC)
    state = load_state()
    
    # NASAデータの遅延を考慮した終了日
    end_date = (now - timedelta(days=DATA_DELAY_DAYS)).strftime("%Y-%m-%d")
    
    logger.info(f"現在: {now.strftime('%Y-%m-%d %H:%M')} (UTC)")
    logger.info(f"データ終了日: {end_date} (NASA遅延 {DATA_DELAY_DAYS}日)")
    logger.info(f"モード: {'全期間一括' if args.full else '差分更新'}")
    
    # 対象サイト
    if args.site:
        sites = [s for s in SITES if s["code"] == args.site]
        if not sites:
            logger.error(f"不明なサイト: {args.site}")
            return
    else:
        sites = SITES
    
    logger.info(f"対象サイト: {len(sites)}箇所")
    
    # 各サイトの取得範囲を決定
    site_ranges = []
    for site in sites:
        code = site["code"]
        
        if args.full:
            start_date = FULL_START_DATE
        else:
            # 差分更新: DB最新日の翌日から
            latest = get_latest_date(code)
            if latest:
                start_date = (latest + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                start_date = FULL_START_DATE
        
        # 開始日 > 終了日 なら更新不要
        if start_date > end_date:
            logger.info(f"  {site['name']} ({code}): 最新 ✅")
            continue
        
        site_ranges.append({
            "site": site,
            "start_date": start_date,
            "end_date": end_date
        })
        logger.info(f"  {site['name']} ({code}): {start_date} 〜 {end_date}")
    
    if not site_ranges:
        logger.info("全サイト最新です")
        if not args.dry_run:
            save_state(state)
            msg = build_telegram_message([], 0, 0, [])
            send_telegram(msg)
        logger.info("=" * 60)
        return
    
    if args.dry_run:
        logger.info("dry-run: 実行しません")
        logger.info("=" * 60)
        return
    
    # 実行
    results = []
    errors = []
    total = 0
    
    for sr in site_ranges:
        site = sr["site"]
        code = site["code"]
        logger.info(f"取得中: {site['name']} ({code})...")
        
        try:
            # NASA ERDDAPから取得
            sst_data = fetch_sst_period(
                sr["start_date"],
                sr["end_date"],
                site["lat"],
                site["lon"]
            )
            logger.info(f"  取得: {len(sst_data)}日分")
            
            if sst_data:
                # レコード整形
                records = [
                    {
                        "site_code": code,
                        "date": d["date"],
                        "sst": d["sst"],
                        "source": "nasa_mur"
                    }
                    for d in sst_data
                ]
                
                # アップロード
                uploaded = supabase_upsert("sst_daily?on_conflict=site_code,date", records)
                logger.info(f"  ✅ {uploaded}件 upsert")
                total += uploaded
                results.append({"site": code, "count": uploaded, "status": "ok"})
                
                # ステート更新
                state["sites"][code] = {
                    "last_date": sst_data[-1]["date"],
                    "last_sst": sst_data[-1]["sst"]
                }
            else:
                logger.warning(f"  データなし")
                results.append({"site": code, "count": 0, "status": "no_data"})
                
        except Exception as e:
            logger.error(f"  ❌ {e}")
            results.append({"site": code, "count": 0, "status": "error", "error": str(e)})
            errors.append({"site": code, "error": str(e)})
        
        time.sleep(1)  # API負荷軽減
    
    elapsed = time.time() - start_time
    
    # ステート保存
    save_state(state)
    
    logger.info(f"完了: 合計 {total}件 ({elapsed:.1f}秒)")
    logger.info("=" * 60)
    
    # Telegram通知
    msg = build_telegram_message(results, total, elapsed, errors)
    send_telegram(msg, is_error=bool(errors))


if __name__ == "__main__":
    main()
