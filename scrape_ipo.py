"""
ipokabu.net (https://ipokabu.net/yotei/) のIPOスケジュール一覧をスクレイピングするスクリプト。

使い方:
    pip install requests beautifulsoup4
    python scrape_ipo.py                     # 一覧のみ取得しコンソールに表示
    python scrape_ipo.py -o ipo.csv          # 一覧をCSVファイルに保存
    python scrape_ipo.py -o ipo.json         # 一覧をJSONファイルに保存
    python scrape_ipo.py --detail -o ipo.csv # 個別IPOページも巡回して詳細項目を追加

対象ページの各IPO銘柄は <table> 内で2行の <tr> を使って表現されている:
  1行目: 上場日(評価) / 証券コード / 企業(rowspan) / ブックビルディング期間 / 仮条件・公開価格 / 状態(rowspan)
  2行目: 市場 / 抽選資金(上限) / 予想利益
このスクリプトはその2行を1レコードにまとめて取得する。

--detail 指定時は、一覧に含まれる各銘柄の個別ページ(例: https://ipokabu.net/ipo/627A)
にアクセスし、以下の詳細項目を追加で取得する:
  上場日, 社名, 証券コード, 評価, 予想利益, 業種, 仮条件確定日, ブックビルディング期間,
  購入期間, 公募株数, 売出株数, O.A分, 吸収金額, オファリングレシオ, 当選口数, 仮条件

-o で指定した出力ファイルが既に存在する場合、証券コードをキーに前回データとマージする:
  ・既存銘柄は行の位置を保ったまま最新の内容に上書きする(評価が「未定」→「A」等の更新に対応)
  ・サイトから消えた銘柄(掲載終了)はデータから削除せずそのまま残す
  ・新規銘柄はデータの末尾に追加する
"""

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://ipokabu.net/yotei/"
OUTPUT_DIR = Path(__file__).resolve().parent

# 出力する証券会社フラグ列: (列名, /syoken/配下のリンクファイル名)
BROKER_COLUMNS = [
    ("SBI証券", "sbi.html"),
    ("野村證券", "t_nomura.html"),
    ("SMBC日興証券", "smbc.html"),
    ("みずほ証券", "t_mizuho.html"),
    ("大和証券", "t_daiwa.html"),
    ("マネックス証券", "monex.html"),
    ("三菱UFJモルガン・スタンレー証券", "z_ufjmoru.html"),
    ("岡三オンライン", "okasano.html"),
    ("楽天証券", "rakuten.html"),
    ("松井証券", "matsui.html"),
    ("大和コネクト証券", "connect.html"),
    ("東海東京証券", "tokai.html"),
    ("SBIネオトレード証券", "livestar.html"),
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_html(url: str = URL, timeout: int = 15) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def clean_text(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(strip=True))


def parse_ipo_list(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    # PC向けテーブル(class="nosp"内)を対象にする。見つからない場合は最初のtableにフォールバック。
    container = soup.select_one("div.nosp table") or soup.select_one("table")
    if container is None:
        return []

    rows = container.find_all("tr")
    records = []
    i = 0
    while i < len(rows):
        cells = rows[i].find_all(["td"])
        # 見出し行(th)や区切りはスキップ
        if not cells or rows[i].find("th"):
            i += 1
            continue

        # 1行目: 日付/評価, 証券コード, 企業, BB期間, 仮条件・公開価格, 状態
        date_td = rows[i].select_one("td[class^='td_t_']")
        code_td = rows[i].select_one("td.td_code2")
        company_td = rows[i].select_one("td.td_kigyo")
        bb_td = rows[i].select_one("td.ipo_yotei2")
        price_td = rows[i].select_one("td.f_sotei")
        status_td = rows[i].select_one("td.ipo_yotei")

        if date_td is None or company_td is None:
            i += 1
            continue

        # 日付と評価ランクを分離
        rank_span = date_td.select_one(".t_m, .t_s, .t_a, .t_b, .t_c, .t_d")
        rank = clean_text(rank_span) if rank_span else ""
        date_text = date_td.get_text(separator="|", strip=True)
        listing_date = date_text.split("|")[0]

        # 企業名と証券会社
        company_link = company_td.select_one("a")
        company_name = clean_text(company_link)
        company_url = company_link["href"] if company_link and company_link.has_attr("href") else ""
        if company_url and company_url.startswith("/"):
            company_url = "https://ipokabu.net" + company_url
        broker_link = company_td.select_one(".ipo_syu a")
        broker = clean_text(broker_link)

        record = {
            "listing_date": listing_date,
            "rank": rank,
            "code": clean_text(code_td),
            "company": company_name,
            "company_url": company_url,
            "broker": broker,
            "bookbuilding_period": clean_text(bb_td),
            "price": clean_text(price_td),
            "status": clean_text(status_td),
            "market": "",
            "subscription_fund": "",
            "expected_profit": "",
        }

        # 2行目(市場/抽選資金/予想利益)を取得
        if i + 1 < len(rows):
            next_cells = rows[i + 1].find_all("td")
            if next_cells and not rows[i + 1].find("th"):
                market_td = rows[i + 1].select_one("td.td_sijo2")
                fund_td = rows[i + 1].select_one("td.td_ipo_tyusen2")
                profit_td = rows[i + 1].select_one("td.td_ipo_soneki")
                record["market"] = clean_text(market_td)
                record["subscription_fund"] = clean_text(fund_td)
                record["expected_profit"] = clean_text(profit_td)
                i += 1  # 2行目を消費

        records.append(record)
        i += 1

    return records


def parse_kv_tables(soup: BeautifulSoup) -> dict:
    """div.ta_syosai_sp 内の <table> にある th/td のペアをすべて key:value として集める。
    同じ見出しが複数回出た場合は最初に見つかったものを優先する。"""
    kv: dict[str, str] = {}
    for table in soup.select("div.ta_syosai_sp table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            i = 0
            while i < len(cells) - 1:
                if cells[i].name == "th":
                    key = clean_text(cells[i])
                    value = clean_text(cells[i + 1])
                    if key and key not in kv:
                        kv[key] = value
                    i += 2
                else:
                    i += 1
    return kv


def parse_underwriters(soup: BeautifulSoup) -> dict:
    """主幹事証券/引受幹事証券/委託幹事証券の各行から、証券会社名とリンク先(/syoken/*.html)を取得する。
    戻り値: {"主幹事証券": [(名前, href), ...], "引受幹事証券": [...], "委託幹事証券": [...]}"""
    category_keys = {
        "syukanji": "主幹事証券",
        "hikiuke": "引受幹事証券",
        "itakukanji": "委託幹事証券",
    }
    result = {label: [] for label in category_keys.values()}
    for tr in soup.select("div.ta_syosai_sp table tr"):
        th = tr.find("th")
        if th is None:
            continue
        th_link = th.find("a")
        if th_link is None or not th_link.has_attr("href"):
            continue
        for key, label in category_keys.items():
            if key in th_link["href"]:
                for a in tr.select("td li a"):
                    name = clean_text(a)
                    href = a.get("href", "")
                    if name:
                        result[label].append((name, href))
                break
    return result


def parse_ipo_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    kv = parse_kv_tables(soup)

    # 評価ランクはアイコン画像のalt属性("A"/"B"/"C"/"D"/"未定"等)から取得
    rank_img = soup.select_one("div.ipo_ico_l img")
    rank = rank_img["alt"].strip() if rank_img and rank_img.has_attr("alt") else ""

    # 社名はパンくずリストの最後の項目から取得
    company_span = soup.select_one(
        'ol[itemtype*="BreadcrumbList"] li[itemprop="itemListElement"]:last-of-type span[itemprop="name"]'
    )
    company = clean_text(company_span)

    underwriters = parse_underwriters(soup)
    all_hrefs = [href for pairs in underwriters.values() for _, href in pairs]

    detail = {
        "上場日": kv.get("上場日", ""),
        "社名": company,
        "証券コード": kv.get("証券コード", ""),
        "評価": rank,
        "予想利益": kv.get("予想利益", ""),
        "業種": kv.get("業種", ""),
        "仮条件確定日": kv.get("仮条件決定日", ""),
        "ブックビルディング期間": kv.get("ブックビルディング期間", ""),
        "購入期間": kv.get("購入期間", ""),
        "公募株数": kv.get("公募株数", ""),
        "売出株数": kv.get("売出株数", ""),
        "O.A分": kv.get("O.A分", ""),
        "吸収金額": kv.get("吸収金額", ""),
        "オファリングレシオ": kv.get("オファリングレシオ", ""),
        "当選口数": kv.get("当選口数", ""),
        "仮条件": kv.get("仮条件", ""),
        "主幹事証券": "、".join(name for name, _ in underwriters["主幹事証券"]),
        "引受幹事証券": "、".join(name for name, _ in underwriters["引受幹事証券"]),
        "委託幹事証券": "、".join(name for name, _ in underwriters["委託幹事証券"]),
    }

    for column_name, href_key in BROKER_COLUMNS:
        detail[column_name] = "1" if any(href_key in href for href in all_hrefs) else ""

    return detail


def enrich_with_details(records: list[dict], delay: float = 1.0) -> list[dict]:
    """一覧の各レコードに、個別IPOページの詳細項目をマージする。"""
    results = []
    for idx, record in enumerate(records):
        url = record.get("company_url")
        detail = {}
        if url:
            try:
                detail_html = fetch_html(url)
                detail = parse_ipo_detail(detail_html)
            except requests.RequestException as e:
                print(f"詳細取得に失敗しました ({url}): {e}", file=sys.stderr)
        # 一覧側の項目(listing_date/code/company/bookbuilding_period/expected_profit等)は
        # 詳細ページの項目(上場日/証券コード/社名/ブックビルディング期間/予想利益等)と重複するため、
        # --detail 指定時は詳細ページの項目のみを採用する。
        results.append(detail)
        if idx < len(records) - 1:
            time.sleep(delay)  # サーバー負荷軽減のための間隔
    return results


def date_sort_key(record: dict) -> tuple:
    """レコードの上場日を (年, 月, 日) のタプルに変換してソート用キーとする。
    年が取得できない場合(一覧のみ取得時の "9/25(金)" 形式)は 0 を年として扱う。
    日付が取得できない場合は最後尾に回るよう大きな値を返す。"""
    date_str = record.get("上場日") or record.get("listing_date") or ""
    m = re.search(r"(?:(\d{4})/)?(\d{1,2})/(\d{1,2})", date_str)
    if not m:
        return (9999, 99, 99)
    year = int(m.group(1)) if m.group(1) else 0
    month = int(m.group(2))
    day = int(m.group(3))
    return (year, month, day)


def save_csv(records: list[dict], path: str) -> None:
    if not records:
        return
    fieldnames = list(records[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def save_json(records: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def save_last_updated() -> None:
    """スクレイピングを実行した日時(JST)を記録する。WEBページ側で「最終更新」として表示する。"""
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    text = f"{now.year}/{now.month}/{now.day} {now.hour}:{now.minute:02d}"
    path = OUTPUT_DIR / "last_updated.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"scraped_at": text}, f, ensure_ascii=False)


def load_existing(path: Path) -> list[dict]:
    """前回出力したファイルを読み込む。存在しない場合は空リストを返す。"""
    if not path.exists():
        return []
    if path.suffix.lower() == ".json":
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def merge_records(existing: list[dict], new_records: list[dict], key_field: str) -> list[dict]:
    """既存データに新しい取得結果をマージする。
    ・キー(証券コード)が既存にあれば、その行の位置はそのままで内容だけ最新の内容に更新する。
    ・キーが既存になければ、新規行として末尾に追加する。
    ・サイト上から消えた銘柄(新しい取得結果に含まれない)は、既存データにそのまま残す。"""
    merged = list(existing)
    index = {r.get(key_field): i for i, r in enumerate(merged) if r.get(key_field)}
    for rec in new_records:
        key = rec.get(key_field)
        if not key:
            continue
        if key in index:
            merged[index[key]] = rec
        else:
            merged.append(rec)
            index[key] = len(merged) - 1
    return merged


def main():
    parser = argparse.ArgumentParser(description="ipokabu.net のIPOスケジュールをスクレイピング")
    parser.add_argument(
        "-o", "--output",
        help="出力ファイル名またはパス (.csv または .json)。"
             "相対パスの場合はスクリプトと同じ IPO管理 フォルダに保存されます。",
    )
    parser.add_argument("--url", default=URL, help="取得対象URL (デフォルト: %(default)s)")
    parser.add_argument(
        "--detail", action="store_true",
        help="各銘柄の個別IPOページも巡回し、詳細項目(社名/評価/予想利益/業種など)を追加取得する",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="個別ページ取得時のリクエスト間隔(秒)。デフォルト: %(default)s",
    )
    args = parser.parse_args()

    html = fetch_html(args.url)
    records = parse_ipo_list(html)

    if not records:
        print("IPO情報が取得できませんでした。ページ構造が変更された可能性があります。", file=sys.stderr)
        sys.exit(1)

    if args.detail:
        records = enrich_with_details(records, delay=args.delay)

    # 新規に取得した銘柄同士は上場日の古い順に並べる(既存データとのマージ時、末尾への追加順に反映される)
    records.sort(key=date_sort_key)

    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = OUTPUT_DIR / out_path

        key_field = "証券コード" if args.detail else "code"
        existing = load_existing(out_path)
        records = merge_records(existing, records, key_field)

        if out_path.suffix.lower() == ".json":
            save_json(records, str(out_path))
        else:
            save_csv(records, str(out_path))
        save_last_updated()
        print(f"{len(records)}件のIPO情報を {out_path} に保存しました。")
    elif args.detail:
        for r in records:
            print(f"[{r.get('上場日', '')}({r.get('評価', '') or '-'})] {r.get('証券コード', '')} {r.get('社名', '')}")
            print(
                f"    業種:{r.get('業種', '')} / 仮条件:{r.get('仮条件', '')} "
                f"/ 仮条件確定日:{r.get('仮条件確定日', '')} / 購入期間:{r.get('購入期間', '')} "
                f"/ 予想利益:{r.get('予想利益', '')}"
            )
            print(
                f"    公募株数:{r.get('公募株数', '')} / 売出株数:{r.get('売出株数', '')} "
                f"/ O.A分:{r.get('O.A分', '')} / 吸収金額:{r.get('吸収金額', '')} "
                f"/ オファリングレシオ:{r.get('オファリングレシオ', '')} / 当選口数:{r.get('当選口数', '')}"
            )
        print(f"\n合計 {len(records)} 件")
    else:
        for r in records:
            print(
                f"[{r['listing_date']}({r['rank'] or '-'})] {r['code']} {r['company']} "
                f"/ 市場:{r['market']} / 価格:{r['price']} / BB:{r['bookbuilding_period']} "
                f"/ 資金:{r['subscription_fund']} / 予想利益:{r['expected_profit']} / 証券:{r['broker']}"
            )
        print(f"\n合計 {len(records)} 件")


if __name__ == "__main__":
    main()
