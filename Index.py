import pymysql
import gradio as gr
import pandas as pd
import os
import re
import tempfile
import threading
import unicodedata
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from config import DB_HOST, DB_USER, DB_PASSWORD, DB_NAME, APP_NAME, BACKUP_DIR

_db_local = threading.local()


def _get_db_connection():
    """每個執行緒各用一條自己的資料庫連線。
    Gradio 會同時用多個執行緒處理請求，如果大家共用同一條連線，查詢結果會互相拿錯，
    連線也會被弄壞（畫面出現「錯誤」、下拉選單出現別的資料表的內容）。
    autocommit=True：每次查詢都看得到最新資料；需要多個寫入一起成功/失敗時，用 mydb.begin() 開交易。"""
    conn = getattr(_db_local, "conn", None)
    if conn is None or not conn.open:
        conn = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME, autocommit=True)
        _db_local.conn = conn
        _db_local.cursor = conn.cursor()
    return conn


class _ThreadLocalDB:
    """讓 mydb / mycursor 照原本的寫法使用，但實際上會轉給目前執行緒自己的連線"""
    def __init__(self, getter):
        self._getter = getter

    def __getattr__(self, name):
        return getattr(self._getter(), name)


mydb = _ThreadLocalDB(_get_db_connection)
mycursor = _ThreadLocalDB(lambda: (_get_db_connection(), _db_local.cursor)[1])


def get_options(item):
    # 同時抓取 id 與 name
    sql = "SELECT id, name FROM %s" % item
    mycursor.execute(sql)
    results = mycursor.fetchall()
    # 組合成 [(名稱, ID)] 的格式，例如 [('越南阿龍水產', 1), ('印尼蟹商', 2)]
    options = [(row[1], row[0]) for row in results]
    return options


def parse_positive_number(val):
    """把輸入轉成數字，格式不對或是負數就回傳 None（不會偷偷變成 0）"""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f < 0:
        return None
    return f


# ========== 進貨 ==========

def get_available_year_months():
    """從 purchases 表抓出所有『真的有資料』的年月組合，由新到舊排序"""
    sql = """
        SELECT DISTINCT YEAR(transaction_time) AS y, MONTH(transaction_time) AS m
        FROM purchases
        WHERE transaction_time IS NOT NULL
        ORDER BY y DESC, m DESC
    """
    mycursor.execute(sql)
    rows = mycursor.fetchall()
    return [(row[0], row[1]) for row in rows]


def get_purchases_table(year, month):
    """依年月查詢進貨紀錄，回傳給畫面顯示用的友善格式（不含原始 id）"""
    sql = """
        SELECT pu.transaction_time, s.name AS 廠商, p.name AS 貨品,
               pu.quantity AS 數量, pu.unit AS 單位,
               pu.unit_price AS 單價, pu.total_price AS 總額,
               cur.name AS 幣別
        FROM purchases pu
        LEFT JOIN suppliers s ON pu.supplier_id = s.id
        LEFT JOIN products p ON pu.product_id = p.id
        LEFT JOIN currency cur ON pu.currency_id = cur.id
        WHERE YEAR(pu.transaction_time) = %s AND MONTH(pu.transaction_time) = %s
        ORDER BY pu.transaction_time DESC, pu.id DESC
    """
    mycursor.execute(sql, (year, int(month)))
    rows = mycursor.fetchall()
    return [list(r) for r in rows]


def get_purchases_raw(year, month):
    """依年月查詢進貨紀錄的原始資料（含 id 與外鍵 id），跟 get_purchases_table 同排序，
    只給修改/刪除時對應用，不會直接顯示在畫面上"""
    sql = """
        SELECT pu.id, pu.supplier_id, pu.product_id, pu.quantity,
               pu.unit_price, pu.total_price, pu.unit, pu.currency_id
        FROM purchases pu
        WHERE YEAR(pu.transaction_time) = %s AND MONTH(pu.transaction_time) = %s
        ORDER BY pu.transaction_time DESC, pu.id DESC
    """
    mycursor.execute(sql, (year, int(month)))
    rows = mycursor.fetchall()
    return [list(r) for r in rows]


def refresh_purchases_table(year, month):
    return gr.update(value=get_purchases_table(year, month)), get_purchases_raw(year, month)


def insert_purchas(supplier_id, product_id, qty, unit_price, total_price, unit, currency_id, year, month):
    if not supplier_id or not product_id:
        display, raw = get_purchases_table(year, month), get_purchases_raw(year, month)
        return gr.update(value=display), "⚠️ 廠商、貨品必須選擇", raw

    qty_f = parse_positive_number(qty)
    price_f = parse_positive_number(unit_price)
    if qty_f is None or price_f is None:
        display, raw = get_purchases_table(year, month), get_purchases_raw(year, month)
        return gr.update(value=display), "⚠️ 數量、單價必須是有效的數字", raw

    total_f = parse_positive_number(total_price)
    if total_f is None:
        total_f = round(qty_f * price_f, 2)

    sql = """
        INSERT INTO purchases
        (supplier_id, product_id, quantity, unit_price, total_price, unit, currency_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    mycursor.execute(sql, (supplier_id, product_id, qty_f, price_f, total_f, unit, currency_id))
    mydb.commit()
    msg = f"✅ 新增成功，共新增 {mycursor.rowcount} 筆進貨紀錄"
    display, raw = get_purchases_table(year, month), get_purchases_raw(year, month)
    return gr.update(value=display), msg, raw


def select_purchase_row(evt: gr.SelectData, raw_data):
    """點選進貨紀錄某一列時，把該筆資料帶回新增表單，方便修改"""
    row_idx = evt.index[0]
    rec_id, supplier_id, product_id, qty, price, total, unit, currency_id = raw_data[row_idx]
    msg = f"已選取編號 {rec_id}，可以修改或刪除"
    return (
        rec_id,
        gr.update(value=supplier_id),
        gr.update(value=product_id),
        str(qty),
        str(price),
        str(total),
        gr.update(value=unit),
        gr.update(value=currency_id),
        msg,
    )


def update_purchase(edit_id, supplier_id, product_id, qty, unit_price, total_price, unit, currency_id, year, month):
    display, raw = get_purchases_table(year, month), get_purchases_raw(year, month)
    if edit_id is None:
        return gr.update(value=display), "⚠️ 請先在下方清單點選一筆資料再修改", raw, edit_id
    if not supplier_id or not product_id:
        return gr.update(value=display), "⚠️ 廠商、貨品必須選擇", raw, edit_id

    qty_f = parse_positive_number(qty)
    price_f = parse_positive_number(unit_price)
    if qty_f is None or price_f is None:
        return gr.update(value=display), "⚠️ 數量、單價必須是有效的數字", raw, edit_id

    total_f = parse_positive_number(total_price)
    if total_f is None:
        total_f = round(qty_f * price_f, 2)

    sql = """
        UPDATE purchases
        SET supplier_id=%s, product_id=%s, quantity=%s, unit_price=%s,
            total_price=%s, unit=%s, currency_id=%s
        WHERE id=%s
    """
    mycursor.execute(sql, (supplier_id, product_id, qty_f, price_f, total_f, unit, currency_id, edit_id))
    mydb.commit()
    msg = f"✅ 修改成功（編號 {edit_id}）"
    display, raw = get_purchases_table(year, month), get_purchases_raw(year, month)
    return gr.update(value=display), msg, raw, None


def delete_purchase(edit_id, year, month):
    display, raw = get_purchases_table(year, month), get_purchases_raw(year, month)
    if edit_id is None:
        return gr.update(value=display), "⚠️ 請先在下方清單點選一筆資料再刪除", raw, edit_id
    try:
        mycursor.execute("DELETE FROM purchases WHERE id=%s", (edit_id,))
        mydb.commit()
        msg = f"✅ 已刪除編號 {edit_id}"
        display, raw = get_purchases_table(year, month), get_purchases_raw(year, month)
        return gr.update(value=display), msg, raw, None
    except pymysql.err.IntegrityError:
        mydb.rollback()
        return gr.update(value=display), "⚠️ 刪除失敗，請稍後再試", raw, edit_id


# ========== 出貨 ==========

def get_available_year_months_sales():
    """從 sales 表抓出所有『真的有資料』的年月組合，由新到舊排序"""
    sql = """
        SELECT DISTINCT YEAR(transaction_time) AS y, MONTH(transaction_time) AS m
        FROM sales
        WHERE transaction_time IS NOT NULL
        ORDER BY y DESC, m DESC
    """
    mycursor.execute(sql)
    rows = mycursor.fetchall()
    return [(row[0], row[1]) for row in rows]


def get_sales_table(year, month):
    """依年月查詢出貨紀錄，回傳給畫面顯示用的友善格式（不含原始 id）"""
    sql = """
        SELECT sa.transaction_time, c.name AS 客戶, p.name AS 貨品,
               sa.quantity AS 數量, sa.unit AS 單位,
               sa.unit_price AS 單價, sa.total_price AS 總額
        FROM sales sa
        LEFT JOIN customers c ON sa.customer_id = c.id
        LEFT JOIN products p ON sa.product_id = p.id
        WHERE YEAR(sa.transaction_time) = %s AND MONTH(sa.transaction_time) = %s
        ORDER BY sa.transaction_time DESC, sa.id DESC
    """
    mycursor.execute(sql, (year, int(month)))
    rows = mycursor.fetchall()
    return [list(r) for r in rows]


def get_sales_raw(year, month):
    """依年月查詢出貨紀錄的原始資料（含 id 與外鍵 id），跟 get_sales_table 同排序，
    只給修改/刪除時對應用，不會直接顯示在畫面上"""
    sql = """
        SELECT sa.id, sa.customer_id, sa.product_id, sa.quantity,
               sa.unit_price, sa.total_price, sa.unit
        FROM sales sa
        WHERE YEAR(sa.transaction_time) = %s AND MONTH(sa.transaction_time) = %s
        ORDER BY sa.transaction_time DESC, sa.id DESC
    """
    mycursor.execute(sql, (year, int(month)))
    rows = mycursor.fetchall()
    return [list(r) for r in rows]


def refresh_sales_table(year, month):
    return gr.update(value=get_sales_table(year, month)), get_sales_raw(year, month)


def insert_sale(customer_id, product_id, qty, unit_price, total_price, unit, year, month):
    if not customer_id or not product_id:
        display, raw = get_sales_table(year, month), get_sales_raw(year, month)
        return gr.update(value=display), "⚠️ 客戶、貨品必須選擇", raw

    qty_f = parse_positive_number(qty)
    price_f = parse_positive_number(unit_price)
    if qty_f is None or price_f is None:
        display, raw = get_sales_table(year, month), get_sales_raw(year, month)
        return gr.update(value=display), "⚠️ 數量、單價必須是有效的數字", raw

    total_f = parse_positive_number(total_price)
    if total_f is None:
        total_f = round(qty_f * price_f, 2)

    sql = """
        INSERT INTO sales
        (customer_id, product_id, quantity, unit_price, total_price, unit)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    mycursor.execute(sql, (customer_id, product_id, qty_f, price_f, total_f, unit))
    mydb.commit()
    msg = f"✅ 新增成功，共新增 {mycursor.rowcount} 筆出貨紀錄"
    display, raw = get_sales_table(year, month), get_sales_raw(year, month)
    return gr.update(value=display), msg, raw


def select_sale_row(evt: gr.SelectData, raw_data):
    """點選出貨紀錄某一列時，把該筆資料帶回新增表單，方便修改"""
    row_idx = evt.index[0]
    rec_id, customer_id, product_id, qty, price, total, unit = raw_data[row_idx]
    msg = f"已選取編號 {rec_id}，可以修改或刪除"
    return (
        rec_id,
        gr.update(value=customer_id),
        gr.update(value=product_id),
        str(qty),
        str(price),
        str(total),
        gr.update(value=unit),
        msg,
    )


def update_sale(edit_id, customer_id, product_id, qty, unit_price, total_price, unit, year, month):
    display, raw = get_sales_table(year, month), get_sales_raw(year, month)
    if edit_id is None:
        return gr.update(value=display), "⚠️ 請先在下方清單點選一筆資料再修改", raw, edit_id
    if not customer_id or not product_id:
        return gr.update(value=display), "⚠️ 客戶、貨品必須選擇", raw, edit_id

    qty_f = parse_positive_number(qty)
    price_f = parse_positive_number(unit_price)
    if qty_f is None or price_f is None:
        return gr.update(value=display), "⚠️ 數量、單價必須是有效的數字", raw, edit_id

    total_f = parse_positive_number(total_price)
    if total_f is None:
        total_f = round(qty_f * price_f, 2)

    sql = """
        UPDATE sales
        SET customer_id=%s, product_id=%s, quantity=%s, unit_price=%s,
            total_price=%s, unit=%s
        WHERE id=%s
    """
    mycursor.execute(sql, (customer_id, product_id, qty_f, price_f, total_f, unit, edit_id))
    mydb.commit()
    msg = f"✅ 修改成功（編號 {edit_id}）"
    display, raw = get_sales_table(year, month), get_sales_raw(year, month)
    return gr.update(value=display), msg, raw, None


def delete_sale(edit_id, year, month):
    display, raw = get_sales_table(year, month), get_sales_raw(year, month)
    if edit_id is None:
        return gr.update(value=display), "⚠️ 請先在下方清單點選一筆資料再刪除", raw, edit_id
    try:
        mycursor.execute("DELETE FROM sales WHERE id=%s", (edit_id,))
        mydb.commit()
        msg = f"✅ 已刪除編號 {edit_id}"
        display, raw = get_sales_table(year, month), get_sales_raw(year, month)
        return gr.update(value=display), msg, raw, None
    except pymysql.err.IntegrityError:
        mydb.rollback()
        return gr.update(value=display), "⚠️ 刪除失敗，請稍後再試", raw, edit_id


# ========== 庫存 ==========

def get_month_cutoff(year, month):
    """回傳『下個月第一天』的日期字串，用來當作『這個月月底之前』的查詢邊界"""
    year, month = int(year), int(month)
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    return f"{next_year}-{next_month:02d}-01"


def get_inventory_year_months():
    """合併進貨、出貨兩張表出現過的年月，供庫存頁的年月選單使用"""
    sql = """
        SELECT y, m FROM (
            SELECT YEAR(transaction_time) AS y, MONTH(transaction_time) AS m FROM purchases WHERE transaction_time IS NOT NULL
            UNION
            SELECT YEAR(transaction_time) AS y, MONTH(transaction_time) AS m FROM sales WHERE transaction_time IS NOT NULL
        ) t
        ORDER BY y DESC, m DESC
    """
    mycursor.execute(sql)
    return [(row[0], row[1]) for row in mycursor.fetchall()]


def get_theoretical_inventory(year, month):
    """計算每個『貨品+單位』組合到『該月月底』為止的理論庫存 = 累計進貨量 - 累計出貨量
    因為公斤跟隻是不同的計量方式，不能加在一起，所以分開統計"""
    cutoff = get_month_cutoff(year, month)

    mycursor.execute(
        "SELECT product_id, unit, SUM(quantity) FROM purchases WHERE transaction_time < %s GROUP BY product_id, unit",
        (cutoff,),
    )
    in_map = {(pid, unit): float(qty) for pid, unit, qty in mycursor.fetchall()}

    mycursor.execute(
        "SELECT product_id, unit, SUM(quantity) FROM sales WHERE transaction_time < %s GROUP BY product_id, unit",
        (cutoff,),
    )
    out_map = {(pid, unit): float(qty) for pid, unit, qty in mycursor.fetchall()}

    mycursor.execute("SELECT id, name FROM products")
    name_map = {pid: name for pid, name in mycursor.fetchall()}

    keys = sorted(set(in_map) | set(out_map), key=lambda k: (k[0], k[1]))
    result = []
    for pid, unit in keys:
        theoretical = in_map.get((pid, unit), 0.0) - out_map.get((pid, unit), 0.0)
        name = name_map.get(pid, f"(未知貨品 #{pid})")
        result.append((pid, unit, name, theoretical))
    return result


def get_actual_counts(year, month):
    """取得某年月已登記的實際盤點數量，回傳 {(product_id, unit): 數量}"""
    sql = "SELECT product_id, unit, actual_quantity FROM inventory_counts WHERE year=%s AND month=%s"
    mycursor.execute(sql, (int(year), int(month)))
    return {(pid, unit): float(qty) for pid, unit, qty in mycursor.fetchall()}


def has_inventory_counts(year, month):
    """判斷該月是否至少登記過一筆實際盤點，供走勢圖區分「未盤點」和「死傷為零」。"""
    mycursor.execute(
        "SELECT 1 FROM inventory_counts WHERE year=%s AND month=%s LIMIT 1",
        (int(year), int(month)),
    )
    return mycursor.fetchone() is not None


def get_inventory_loss_summary(year, month):
    """回傳某月盤點死傷的 (公斤, 台幣估值)。

    只納入單位為「公斤」且實際盤點低於理論庫存的正向差額；
    「隻」不計入死傷趨勢與估值。公斤死傷以截至該月底的加權平均進貨成本估值。
    """
    theoretical = {(pid, unit): qty for pid, unit, _name, qty in get_theoretical_inventory(year, month)}
    actual = get_actual_counts(year, month)
    if not actual:
        return 0.0, 0.0

    cutoff = get_month_cutoff(year, month)
    mycursor.execute(
        """
        SELECT id, product_id, unit, currency_id, total_price, quantity,
               YEAR(transaction_time), MONTH(transaction_time)
        FROM purchases
        WHERE transaction_time < %s AND quantity > 0
        """,
        (cutoff,),
    )
    cost_sum, qty_sum = {}, {}
    for purchase_id, product_id, unit, currency_id, total_price, quantity, p_year, p_month in mycursor.fetchall():
        twd, _is_estimated = get_purchase_twd_value(
            purchase_id, currency_id, total_price, p_year, p_month
        )
        key = (product_id, unit)
        cost_sum[key] = cost_sum.get(key, 0.0) + twd
        qty_sum[key] = qty_sum.get(key, 0.0) + float(quantity)

    loss_kg = 0.0
    loss_twd = 0.0
    for key, actual_qty in actual.items():
        if key[1] != "公斤":
            continue
        loss_qty = theoretical.get(key, 0.0) - actual_qty
        if loss_qty <= 0 or qty_sum.get(key, 0.0) <= 0:
            continue
        average_cost = cost_sum[key] / qty_sum[key]
        loss_kg += loss_qty
        loss_twd += loss_qty * average_cost
    return round(loss_kg, 2), round(loss_twd, 2)


def get_yearly_inventory_loss_summary(year):
    """加總該年各月份已有盤點資料的死傷公斤與估值。"""
    mycursor.execute(
        "SELECT DISTINCT month FROM inventory_counts WHERE year=%s ORDER BY month",
        (int(year),),
    )
    totals = [get_inventory_loss_summary(year, month) for (month,) in mycursor.fetchall()]
    return round(sum(item[0] for item in totals), 2), round(sum(item[1] for item in totals), 2)


def get_inventory_table(year, month):
    """組合理論庫存與實際盤點，回傳畫面顯示用的表格，以及修改/刪除用的原始資料"""
    theoretical_list = get_theoretical_inventory(year, month)
    actual_map = get_actual_counts(year, month)

    display_rows = []
    raw_rows = []
    for pid, unit, name, theo in theoretical_list:
        actual = actual_map.get((pid, unit))
        if actual is None:
            display_rows.append([name, unit, theo, "尚未盤點", "-"])
        else:
            diff = round(theo - actual, 2)
            display_rows.append([name, unit, theo, actual, diff])
        raw_rows.append([pid, unit, theo, actual])
    return display_rows, raw_rows


def refresh_inventory_table(year, month):
    display, raw = get_inventory_table(year, month)
    return gr.update(value=display), raw


def select_inventory_row(evt: gr.SelectData, raw_data):
    """點選庫存清單某一列時，把該貨品與單位帶入下方的盤點登記表單。
    同時從畫面上的資料裡找出同一貨品『另一個單位』的已登記數字存起來，
    這樣之後在表單裡切換單位時，可以自動帶出對應的實際盤點數量"""
    row_idx = evt.index[0]
    pid, unit, theo, actual = raw_data[row_idx]

    other_unit = "隻" if unit == "公斤" else "公斤"
    other_actual = None
    for r in raw_data:
        if r[0] == pid and r[1] == other_unit:
            other_actual = r[3]
            break
    state = {unit: actual, other_unit: other_actual}

    actual_str = "" if actual is None else str(actual)
    msg = f"已選取此貨品（單位：{unit}），理論庫存為 {theo}，切換單位會自動帶出對應的實際盤點數量"
    return gr.update(value=pid), gr.update(value=unit), actual_str, msg, state


def switch_inventory_unit(unit, state):
    """切換盤點表單的單位時，自動帶出這個貨品在該單位下已登記的實際數量"""
    if not state:
        return gr.update()
    actual = state.get(unit)
    return "" if actual is None else str(actual)


def save_inventory_count(product_id, unit, actual_qty, year, month):
    display, raw = get_inventory_table(year, month)
    if not product_id or not unit:
        return gr.update(value=display), "⚠️ 請先選擇貨品與單位", raw

    qty_f = parse_positive_number(actual_qty)
    if qty_f is None:
        return gr.update(value=display), "⚠️ 實際盤點數量必須是有效的數字", raw

    sql = """
        INSERT INTO inventory_counts (product_id, unit, year, month, actual_quantity)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE actual_quantity = VALUES(actual_quantity), recorded_at = CURRENT_TIMESTAMP
    """
    mycursor.execute(sql, (product_id, unit, int(year), int(month), qty_f))
    mydb.commit()
    msg = "✅ 盤點已登記"
    display, raw = get_inventory_table(year, month)
    return gr.update(value=display), msg, raw


def clear_inventory_count(product_id, unit, year, month):
    display, raw = get_inventory_table(year, month)
    if not product_id or not unit:
        return gr.update(value=display), "⚠️ 請先選擇貨品與單位", raw
    mycursor.execute(
        "DELETE FROM inventory_counts WHERE product_id=%s AND unit=%s AND year=%s AND month=%s",
        (product_id, unit, int(year), int(month)),
    )
    mydb.commit()
    msg = "✅ 已清除此筆盤點紀錄（恢復成尚未盤點）"
    display, raw = get_inventory_table(year, month)
    return gr.update(value=display), msg, raw


def refresh_inventory_product_dropdown():
    """點進『庫存』分頁時，重新查一次貨品下拉選單"""
    return gr.update(choices=get_options("products"))



# ========== 匯率 ==========

def is_base_currency(currency_id):
    """判斷這個幣別是不是台幣（名稱為 TWD / 台幣 / 新台幣），台幣不需要登記匯率，直接視為 1:1"""
    mycursor.execute("SELECT name FROM currency WHERE id=%s", (currency_id,))
    row = mycursor.fetchone()
    if not row:
        return False
    name = row[0].strip().upper()
    return name in ("TWD", "台幣", "新台幣", "NTD")


# 常見幣別對台幣的「大概」匯率，只在使用者還沒登記真實匯率時當備用估計值，
# 這些數字不會自動更新、也不保證準確，只是先讓報表有個粗略數字可以看，
# 有登記真實匯率時一律優先使用真實登記的匯率。
DEFAULT_RATE_GUESSES = {
    "USD": 32.0, "美金": 32.0, "美元": 32.0,
    "CNY": 4.4, "RMB": 4.4, "人民幣": 4.4,
    "VND": 0.0013, "越南盾": 0.0013,
    "IDR": 0.002, "印尼盾": 0.002,
    "PHP": 0.57, "披索": 0.57, "菲律賓披索": 0.57,
    "JPY": 0.21, "日圓": 0.21, "日幣": 0.21,
    "EUR": 34.8, "歐元": 34.8,
    "HKD": 4.1, "港幣": 4.1,
}


def get_currency_name(currency_id):
    mycursor.execute("SELECT name FROM currency WHERE id=%s", (currency_id,))
    row = mycursor.fetchone()
    return row[0] if row else f"#{currency_id}"


def get_default_rate_guess(currency_id):
    """查內建的大概匯率對照表；找不到就回傳 None"""
    name = get_currency_name(currency_id).strip().upper()
    return DEFAULT_RATE_GUESSES.get(name)


def get_exchange_rate(currency_id, year, month):
    """取得某幣別在某年月對台幣的匯率。
    回傳 (匯率, 是否為估計值)：
      - 台幣：(1.0, False)
      - 有登記真實匯率：(登記的匯率, False)
      - 沒登記但內建有大概匯率：(大概匯率, True)
      - 完全沒有依據：(1.0, True) 當最後手段"""
    if is_base_currency(currency_id):
        return 1.0, False
    mycursor.execute(
        "SELECT rate_to_twd FROM exchange_rates WHERE currency_id=%s AND year=%s AND month=%s",
        (currency_id, int(year), int(month)),
    )
    row = mycursor.fetchone()
    if row:
        return float(row[0]), False
    guess = get_default_rate_guess(currency_id)
    if guess is not None:
        return guess, True
    return 1.0, True


def get_exchange_rate_list():
    """取得所有已登記的匯率，供畫面顯示與選取"""
    sql = """
        SELECT er.id, cur.name, er.year, er.month, er.rate_to_twd, er.currency_id
        FROM exchange_rates er
        LEFT JOIN currency cur ON er.currency_id = cur.id
        ORDER BY er.year DESC, er.month DESC, cur.name
    """
    mycursor.execute(sql)
    display_rows, raw_rows = [], []
    for rid, cname, y, m, rate, currency_id in mycursor.fetchall():
        display_rows.append([cname, y, m, float(rate)])
        raw_rows.append([rid, currency_id, y, m, float(rate)])
    return display_rows, raw_rows


def refresh_exchange_rate_list():
    display, raw = get_exchange_rate_list()
    return gr.update(value=display), raw


def select_exchange_rate_row(evt: gr.SelectData, raw_data):
    row_idx = evt.index[0]
    rid, currency_id, year, month, rate = raw_data[row_idx]
    msg = f"已選取此筆匯率紀錄，可以修改或刪除"
    return (
        rid,
        gr.update(value=currency_id),
        str(year),
        f"{int(month):02d}",
        str(rate),
        msg,
    )


def save_exchange_rate(edit_id, currency_id, year, month, rate):
    display, raw = get_exchange_rate_list()
    if not currency_id or not year or not month:
        return gr.update(value=display), "⚠️ 幣別、年份、月份都必須填寫", raw, edit_id

    rate_f = parse_positive_number(rate)
    if rate_f is None:
        return gr.update(value=display), "⚠️ 匯率必須是有效的正數字", raw, edit_id

    if is_base_currency(currency_id):
        return gr.update(value=display), "⚠️ 這個幣別是台幣本身，不需要登記匯率", raw, edit_id

    sql = """
        INSERT INTO exchange_rates (currency_id, year, month, rate_to_twd)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE rate_to_twd = VALUES(rate_to_twd), recorded_at = CURRENT_TIMESTAMP
    """
    mycursor.execute(sql, (currency_id, int(year), int(month), rate_f))
    mydb.commit()
    msg = "✅ 匯率已登記"
    display, raw = get_exchange_rate_list()
    return gr.update(value=display), msg, raw, None


def delete_exchange_rate(edit_id):
    display, raw = get_exchange_rate_list()
    if edit_id is None:
        return gr.update(value=display), "⚠️ 請先在下方清單點選一筆資料再刪除", raw, edit_id
    mycursor.execute("DELETE FROM exchange_rates WHERE id=%s", (edit_id,))
    mydb.commit()
    msg = "✅ 已刪除此筆匯率紀錄"
    display, raw = get_exchange_rate_list()
    return gr.update(value=display), msg, raw, None


# ========== 儀表板 ==========

def get_sales_total(year, month=None):
    """出貨總額（假設全部都是台幣交易，不用換算）"""
    if month is not None:
        sql = "SELECT SUM(total_price) FROM sales WHERE YEAR(transaction_time)=%s AND MONTH(transaction_time)=%s"
        params = (int(year), int(month))
    else:
        sql = "SELECT SUM(total_price) FROM sales WHERE YEAR(transaction_time)=%s"
        params = (int(year),)
    mycursor.execute(sql, params)
    row = mycursor.fetchone()
    return float(row[0]) if row[0] is not None else 0.0


def get_purchases_total_twd(year, month=None):
    """進貨總額換算成台幣。每一筆優先使用『已透過應付帳款實際分配』換算出來的台幣金額
    （來自實際匯款反推的真實匯率），沒有分配過的才用該月的登記/估計匯率去算。
    回傳 (總額, 使用估計匯率的幣別名稱集合)"""
    if month is not None:
        sql = """
            SELECT id, currency_id, total_price, MONTH(transaction_time) AS m
            FROM purchases WHERE YEAR(transaction_time)=%s AND MONTH(transaction_time)=%s
        """
        params = (int(year), int(month))
    else:
        sql = """
            SELECT id, currency_id, total_price, MONTH(transaction_time) AS m
            FROM purchases WHERE YEAR(transaction_time)=%s
        """
        params = (int(year),)
    mycursor.execute(sql, params)
    total = 0.0
    estimated_currencies = set()
    for pid, currency_id, amt, m in mycursor.fetchall():
        twd, is_estimated = get_purchase_twd_value(pid, currency_id, amt, year, m)
        if is_estimated:
            estimated_currencies.add(get_currency_name(currency_id))
        total += twd
    return total, estimated_currencies


def get_expenses_total(year, month=None):
    if month is not None:
        sql = "SELECT SUM(amount) FROM monthly_expenses WHERE year=%s AND month=%s"
        params = (int(year), int(month))
    else:
        sql = "SELECT SUM(amount) FROM monthly_expenses WHERE year=%s"
        params = (int(year),)
    mycursor.execute(sql, params)
    row = mycursor.fetchone()
    return float(row[0]) if row[0] is not None else 0.0


def get_monthly_summary(year, month):
    sales = get_sales_total(year, month)
    purchases, estimated_currencies = get_purchases_total_twd(year, month)
    gross = round(sales - purchases, 2)
    expenses = get_expenses_total(year, month)
    net = round(gross - expenses, 2)
    return sales, purchases, gross, expenses, net, estimated_currencies


def format_dashboard_summary(year, month):
    sales, purchases, gross, expenses, net, estimated_currencies = get_monthly_summary(year, month)
    inventory_loss_kg, inventory_loss_twd = get_inventory_loss_summary(year, month)
    warning = ""
    if estimated_currencies:
        names = "、".join(sorted(estimated_currencies))
        warning = (
            f"\n\n⚠️ 這個月的「{names}」還沒登記真實匯率，"
            f"暫時用系統內建的大概匯率試算，數字僅供參考，請到「基礎資料設定 → 匯率」補上正確匯率後會自動改用真實數字。"
        )
    text = (
        f"### {year} 年 {int(month):02d} 月 結算\n\n"
        f"- 出貨總額：{sales:,.2f}\n"
        f"- 進貨總額（已換算台幣）：{purchases:,.2f}\n"
        f"- **毛利（出貨 - 進貨）：{gross:,.2f}**\n"
        f"- 盤點死傷（僅公斤、已盤點且短少）：{inventory_loss_kg:,.2f} 公斤（估值 {inventory_loss_twd:,.2f} 元）\n"
        f"- 其他支出（薪水/租金等）：{expenses:,.2f}\n"
        f"- **淨利（毛利 - 其他支出）：{net:,.2f}**"
        f"{warning}"
    )
    return text


def get_expenses_table(year, month):
    sql = "SELECT id, item_name, amount FROM monthly_expenses WHERE year=%s AND month=%s ORDER BY id"
    mycursor.execute(sql, (int(year), int(month)))
    rows = mycursor.fetchall()
    display_rows = [[name, float(amt)] for _, name, amt in rows]
    raw_rows = [[rid, name, float(amt)] for rid, name, amt in rows]
    return display_rows, raw_rows


def refresh_dashboard(year, month):
    summary_text = format_dashboard_summary(year, month)
    display, raw = get_expenses_table(year, month)
    return summary_text, gr.update(value=display), raw


def select_expense_row(evt: gr.SelectData, raw_data):
    row_idx = evt.index[0]
    rid, name, amt = raw_data[row_idx]
    return rid, name, str(amt), "已選取此筆支出，可以修改或刪除"


def insert_expense(year, month, name, amount):
    name = (name or "").strip()
    display, raw = get_expenses_table(year, month)
    if not name:
        return format_dashboard_summary(year, month), gr.update(value=display), raw, "⚠️ 項目名稱不可空白", "", ""
    amt_f = parse_positive_number(amount)
    if amt_f is None:
        return format_dashboard_summary(year, month), gr.update(value=display), raw, "⚠️ 金額必須是有效的數字", name, amount

    mycursor.execute(
        "INSERT INTO monthly_expenses (year, month, item_name, amount) VALUES (%s, %s, %s, %s)",
        (int(year), int(month), name, amt_f),
    )
    mydb.commit()
    display, raw = get_expenses_table(year, month)
    return format_dashboard_summary(year, month), gr.update(value=display), raw, "✅ 新增成功", "", ""


def update_expense(edit_id, year, month, name, amount):
    name = (name or "").strip()
    display, raw = get_expenses_table(year, month)
    if edit_id is None:
        return format_dashboard_summary(year, month), gr.update(value=display), raw, "⚠️ 請先選取一筆支出再修改", edit_id, name, amount
    if not name:
        return format_dashboard_summary(year, month), gr.update(value=display), raw, "⚠️ 項目名稱不可空白", edit_id, name, amount
    amt_f = parse_positive_number(amount)
    if amt_f is None:
        return format_dashboard_summary(year, month), gr.update(value=display), raw, "⚠️ 金額必須是有效的數字", edit_id, name, amount

    mycursor.execute(
        "UPDATE monthly_expenses SET item_name=%s, amount=%s WHERE id=%s",
        (name, amt_f, edit_id),
    )
    mydb.commit()
    display, raw = get_expenses_table(year, month)
    return format_dashboard_summary(year, month), gr.update(value=display), raw, "✅ 修改成功", None, "", ""


def delete_expense(edit_id, year, month):
    display, raw = get_expenses_table(year, month)
    if edit_id is None:
        return format_dashboard_summary(year, month), gr.update(value=display), raw, "⚠️ 請先選取一筆支出再刪除", edit_id, "", ""
    mycursor.execute("DELETE FROM monthly_expenses WHERE id=%s", (edit_id,))
    mydb.commit()
    display, raw = get_expenses_table(year, month)
    return format_dashboard_summary(year, month), gr.update(value=display), raw, "✅ 已刪除", None, "", ""


def get_trend_periods(mode):
    """依檢視模式回傳 (year, month_or_None, label) 的清單，由舊到新排序，方便畫圖"""
    now = datetime.now()
    periods = []
    if mode == "近12個月":
        y, m = now.year, now.month
        for _ in range(12):
            periods.append((y, m, f"{y}/{m:02d}"))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        periods.reverse()
    elif mode == "近5年":
        for i in range(4, -1, -1):
            y = now.year - i
            periods.append((y, None, str(y)))
    else:  # 近10年
        for i in range(9, -1, -1):
            y = now.year - i
            periods.append((y, None, str(y)))
    return periods


def get_trend_chart_data(mode):
    periods = get_trend_periods(mode)
    rows = []
    for y, m, label in periods:
        sales, purchases, gross, expenses, net, _ = get_monthly_summary(y, m) if m else (
            get_sales_total(y),
            get_purchases_total_twd(y)[0],
            None,
            get_expenses_total(y),
            None,
            None,
        )
        if m:
            gross_val, net_val = gross, net
            # 未盤點的月份不畫成 0，避免誤解為「沒有死傷」。
            loss_kg, loss_val = get_inventory_loss_summary(y, m) if has_inventory_counts(y, m) else (None, None)
        else:
            gross_val = round(sales - purchases, 2)
            net_val = round(gross_val - expenses, 2)
            loss_kg, loss_val = get_yearly_inventory_loss_summary(y)
        rows.append({"期間": label, "類別": "出貨", "金額": sales})
        rows.append({"期間": label, "類別": "進貨", "金額": purchases})
        rows.append({"期間": label, "類別": "毛利", "金額": gross_val})
        rows.append({"期間": label, "類別": "淨利", "金額": net_val})
        rows.append({"期間": label, "類別": "死傷估值", "金額": loss_val})
    return pd.DataFrame(rows)


def get_inventory_loss_trend_data(mode):
    """死傷數量趨勢只顯示公斤，與台幣金額趨勢分開繪製。"""
    rows = []
    for y, m, label in get_trend_periods(mode):
        if m:
            loss_kg = get_inventory_loss_summary(y, m)[0] if has_inventory_counts(y, m) else None
        else:
            loss_kg = get_yearly_inventory_loss_summary(y)[0]
        rows.append({"期間": label, "類別": "死傷（公斤）", "公斤": loss_kg})
    return pd.DataFrame(rows)


LOSS_KG_LABEL = "死傷（公斤）"


def refresh_trend_chart(mode, selected):
    """切換檢視方式時重新查一次資料，並依目前勾選的線條過濾後畫出來。
    完整資料也存進 State，之後只勾選/取消勾選時不用再查資料庫。"""
    main_df = get_trend_chart_data(mode)
    loss_df = get_inventory_loss_trend_data(mode)
    selected = selected or []
    return (
        gr.update(value=main_df[main_df["類別"].isin(selected)]),
        gr.update(value=loss_df),
        gr.update(visible=LOSS_KG_LABEL in selected),
        main_df,
        loss_df,
    )


def apply_trend_filter(selected, main_df, loss_df):
    """勾選/取消勾選線條時，直接用已經算好的資料過濾，不用重新查資料庫"""
    if main_df is None:
        return gr.update(), gr.update()
    selected = selected or []
    return (
        gr.update(value=main_df[main_df["類別"].isin(selected)]),
        gr.update(visible=LOSS_KG_LABEL in selected),
    )


def refresh_dashboard_year_choices():
    """點進『儀表板』分頁時，重新查一次年份選單，確保有最新一年可以選"""
    year_months = get_inventory_year_months()
    years = sorted({str(y) for y, m in year_months}, reverse=True)
    if not years:
        years = [str(datetime.now().year)]
    return gr.update(choices=years)


# ========== 應收帳款 ==========

def get_customer_balances():
    """每個客戶：累計出貨總額、累計已收金額、應收餘額"""
    mycursor.execute("SELECT customer_id, SUM(total_price) FROM sales GROUP BY customer_id")
    sales_map = {cid: float(amt) for cid, amt in mycursor.fetchall()}
    mycursor.execute("SELECT customer_id, SUM(amount) FROM receivable_payments GROUP BY customer_id")
    paid_map = {cid: float(amt) for cid, amt in mycursor.fetchall()}
    mycursor.execute("SELECT id, name FROM customers")
    name_map = {cid: name for cid, name in mycursor.fetchall()}

    rows = []
    for cid in set(sales_map) | set(paid_map):
        total = sales_map.get(cid, 0.0)
        paid = paid_map.get(cid, 0.0)
        rows.append([name_map.get(cid, f"(未知客戶 #{cid})"), total, paid, round(total - paid, 2)])
    rows.sort(key=lambda r: r[0])
    return rows


def get_receivable_payments_list():
    sql = """
        SELECT rp.id, c.name, rp.payment_date, rp.amount, rp.note, rp.customer_id
        FROM receivable_payments rp
        LEFT JOIN customers c ON rp.customer_id = c.id
        ORDER BY rp.payment_date DESC, rp.id DESC
    """
    mycursor.execute(sql)
    display_rows, raw_rows = [], []
    for rid, cname, pdate, amount, note, customer_id in mycursor.fetchall():
        display_rows.append([cname, str(pdate), float(amount), note or ""])
        raw_rows.append([rid, customer_id, str(pdate), float(amount), note or ""])
    return display_rows, raw_rows


def refresh_receivable_page():
    balance_display = get_customer_balances()
    payment_display, payment_raw = get_receivable_payments_list()
    return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw


def parse_date_str(val):
    val = (val or "").strip()
    try:
        datetime.strptime(val, "%Y-%m-%d")
        return val
    except ValueError:
        return None


def select_receivable_row(evt: gr.SelectData, raw_data):
    row_idx = evt.index[0]
    rid, customer_id, pdate, amount, note = raw_data[row_idx]
    msg = "已選取此筆收款紀錄，可以修改或刪除"
    return rid, gr.update(value=customer_id), pdate, str(amount), note, msg


def insert_receivable_payment(customer_id, pdate, amount, note):
    balance_display = get_customer_balances()
    payment_display, payment_raw = get_receivable_payments_list()
    if not customer_id:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 請選擇客戶"
    date_ok = parse_date_str(pdate)
    if date_ok is None:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 日期格式必須是 YYYY-MM-DD"
    amt_f = parse_positive_number(amount)
    if amt_f is None:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 金額必須是有效的數字"

    mycursor.execute(
        "INSERT INTO receivable_payments (customer_id, payment_date, amount, note) VALUES (%s, %s, %s, %s)",
        (customer_id, date_ok, amt_f, (note or "").strip() or None),
    )
    mydb.commit()
    balance_display = get_customer_balances()
    payment_display, payment_raw = get_receivable_payments_list()
    return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "✅ 新增成功"


def update_receivable_payment(edit_id, customer_id, pdate, amount, note):
    balance_display = get_customer_balances()
    payment_display, payment_raw = get_receivable_payments_list()
    if edit_id is None:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 請先選取一筆紀錄再修改", edit_id
    if not customer_id:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 請選擇客戶", edit_id
    date_ok = parse_date_str(pdate)
    if date_ok is None:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 日期格式必須是 YYYY-MM-DD", edit_id
    amt_f = parse_positive_number(amount)
    if amt_f is None:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 金額必須是有效的數字", edit_id

    mycursor.execute(
        "UPDATE receivable_payments SET customer_id=%s, payment_date=%s, amount=%s, note=%s WHERE id=%s",
        (customer_id, date_ok, amt_f, (note or "").strip() or None, edit_id),
    )
    mydb.commit()
    balance_display = get_customer_balances()
    payment_display, payment_raw = get_receivable_payments_list()
    return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "✅ 修改成功", None


def delete_receivable_payment(edit_id):
    balance_display = get_customer_balances()
    payment_display, payment_raw = get_receivable_payments_list()
    if edit_id is None:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 請先選取一筆紀錄再刪除", edit_id
    mycursor.execute("DELETE FROM receivable_payments WHERE id=%s", (edit_id,))
    mydb.commit()
    balance_display = get_customer_balances()
    payment_display, payment_raw = get_receivable_payments_list()
    return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "✅ 已刪除", None


def refresh_receivable_customer_dropdown():
    return gr.update(choices=get_options("customers"))


# ========== 應付帳款 ==========

def get_purchase_twd_value(purchase_id, currency_id, total_price, year, month):
    """單筆進貨的台幣價值：優先用『實際分配過的付款』換算出來的台幣金額，
    沒有分配過的才用該月的登記/估計匯率去算"""
    mycursor.execute("SELECT twd_amount FROM payable_payment_allocations WHERE purchase_id=%s", (purchase_id,))
    row = mycursor.fetchone()
    if row:
        return float(row[0]), False
    rate, is_estimated = get_exchange_rate(currency_id, year, month)
    return float(total_price) * rate, is_estimated


def get_supplier_balances():
    """每個廠商：累計進貨總額（換算台幣）、累計已付金額（台幣）、應付餘額（台幣）"""
    mycursor.execute("SELECT id, supplier_id, currency_id, total_price, YEAR(transaction_time), MONTH(transaction_time) FROM purchases")
    purchase_rows = mycursor.fetchall()

    purchase_twd_map = {}
    for pid, sid, currency_id, total_price, y, m in purchase_rows:
        twd, _ = get_purchase_twd_value(pid, currency_id, total_price, y, m)
        purchase_twd_map[sid] = purchase_twd_map.get(sid, 0.0) + twd

    mycursor.execute("SELECT supplier_id, SUM(amount) FROM payable_payments GROUP BY supplier_id")
    paid_map = {sid: float(amt) for sid, amt in mycursor.fetchall()}
    mycursor.execute("SELECT id, name FROM suppliers")
    name_map = {sid: name for sid, name in mycursor.fetchall()}

    rows = []
    for sid in set(purchase_twd_map) | set(paid_map):
        total = round(purchase_twd_map.get(sid, 0.0), 2)
        paid = paid_map.get(sid, 0.0)
        rows.append([name_map.get(sid, f"(未知廠商 #{sid})"), total, paid, round(total - paid, 2)])
    rows.sort(key=lambda r: r[0])
    return rows


def get_payable_payments_list():
    sql = """
        SELECT pp.id, s.name, pp.payment_date, pp.amount, pp.exchange_rate, pp.note, pp.supplier_id,
               GROUP_CONCAT(DISTINCT cur.name ORDER BY cur.name SEPARATOR ', ') AS currency_names,
               SUM(ppa.original_amount) AS foreign_total
        FROM payable_payments pp
        LEFT JOIN suppliers s ON pp.supplier_id = s.id
        LEFT JOIN payable_payment_allocations ppa ON ppa.payment_id = pp.id
        LEFT JOIN purchases pu ON ppa.purchase_id = pu.id
        LEFT JOIN currency cur ON pu.currency_id = cur.id
        GROUP BY pp.id, s.name, pp.payment_date, pp.amount, pp.exchange_rate, pp.note, pp.supplier_id
        ORDER BY pp.payment_date DESC, pp.id DESC
    """
    mycursor.execute(sql)
    display_rows, raw_rows = [], []
    for rid, sname, pdate, amount, rate, note, supplier_id, currencies, foreign_total in mycursor.fetchall():
        rate_disp = round(float(rate), 4) if rate is not None else "-"
        currency_disp = currencies or "未分配（舊資料）"
        foreign_disp = round(float(foreign_total), 2) if foreign_total is not None else "-"
        display_rows.append([sname, str(pdate), currency_disp, foreign_disp, float(amount), rate_disp, note or ""])
        raw_rows.append([rid, supplier_id, str(pdate), float(amount), note or ""])
    return display_rows, raw_rows


def get_payment_allocation_detail(payment_id):
    """查某一筆付款分配到了哪些進貨紀錄，供點選付款紀錄時顯示明細"""
    if payment_id is None:
        return []
    sql = """
        SELECT pu.transaction_time, p.name, ppa.original_amount, cur.name, ppa.effective_rate, ppa.twd_amount
        FROM payable_payment_allocations ppa
        LEFT JOIN purchases pu ON ppa.purchase_id = pu.id
        LEFT JOIN products p ON pu.product_id = p.id
        LEFT JOIN currency cur ON pu.currency_id = cur.id
        WHERE ppa.payment_id = %s
        ORDER BY pu.transaction_time
    """
    mycursor.execute(sql, (payment_id,))
    return [[str(t), pname, float(amt), cname, round(float(rate), 4), float(twd)]
            for t, pname, amt, cname, rate, twd in mycursor.fetchall()]


def refresh_payable_page():
    balance_display = get_supplier_balances()
    payment_display, payment_raw = get_payable_payments_list()
    return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw


def select_payable_row(evt: gr.SelectData, raw_data):
    row_idx = evt.index[0]
    rid, supplier_id, pdate, amount, note = raw_data[row_idx]
    detail = get_payment_allocation_detail(rid)
    if detail:
        msg = "已選取此筆付款紀錄，下方顯示這筆付款分配到的進貨明細（此類付款只能刪除，不能修改）"
    else:
        msg = "已選取此筆付款紀錄，可以刪除"
    return rid, gr.update(value=detail), msg


def get_unallocated_payment_currencies(supplier_id):
    """列出某廠商仍有未付款進貨的幣別；一次付款只能處理一個幣別。"""
    if not supplier_id:
        return []
    sql = """
        SELECT DISTINCT cur.id, cur.name
        FROM purchases pu
        LEFT JOIN currency cur ON pu.currency_id = cur.id
        WHERE pu.supplier_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM payable_payment_allocations ppa WHERE ppa.purchase_id = pu.id
          )
        ORDER BY cur.name
    """
    mycursor.execute(sql, (supplier_id,))
    return [(name or f"幣別 #{currency_id}", currency_id) for currency_id, name in mycursor.fetchall()]


def get_unallocated_purchases(supplier_id, currency_id):
    """某廠商、某幣別還沒被分配過的進貨紀錄。"""
    if not supplier_id or not currency_id:
        return []
    sql = """
        SELECT pu.id, pu.transaction_time, p.name, pu.quantity, pu.unit, pu.total_price, cur.name
        FROM purchases pu
        LEFT JOIN products p ON pu.product_id = p.id
        LEFT JOIN currency cur ON pu.currency_id = cur.id
        WHERE pu.supplier_id = %s
          AND pu.currency_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM payable_payment_allocations ppa WHERE ppa.purchase_id = pu.id
          )
        ORDER BY pu.transaction_time
    """
    mycursor.execute(sql, (supplier_id, currency_id))
    return mycursor.fetchall()


def refresh_payment_currency_choices(supplier_id):
    choices = get_unallocated_payment_currencies(supplier_id)
    hint = "請先選擇付款幣別，再勾選要合併付款的進貨。" if choices else "此廠商目前沒有尚未分配的進貨紀錄。"
    return gr.update(choices=choices, value=None), gr.update(choices=[], value=[]), hint


def refresh_unallocated_choices(supplier_id, currency_id):
    rows = get_unallocated_purchases(supplier_id, currency_id)
    choices = []
    for pid, tdate, pname, qty, unit, total, cname in rows:
        label = f"{tdate}｜{pname}｜{qty}{unit}｜{float(total):,.2f} {cname}"
        choices.append((label, pid))
    currency_name = get_currency_name(currency_id) if currency_id else ""
    hint = f"請勾選本次要付款的 {currency_name} 進貨紀錄。" if choices else "此幣別沒有尚未分配的進貨紀錄。"
    return gr.update(choices=choices, value=[]), hint


def preview_payable_payment(supplier_id, currency_id, selected_ids, twd_total):
    """在真正寫入前顯示本次付款會使用的匯率與外幣合計。"""
    if not supplier_id:
        return "請先選擇廠商。"
    if not currency_id:
        return "請選擇本次付款的幣別。"
    if not selected_ids:
        return "請勾選要合併付款的進貨紀錄。"
    placeholders = ",".join(["%s"] * len(selected_ids))
    sql = f"""
        SELECT COUNT(*), COALESCE(SUM(total_price), 0)
        FROM purchases
        WHERE supplier_id=%s AND currency_id=%s AND id IN ({placeholders})
    """
    mycursor.execute(sql, (supplier_id, currency_id, *selected_ids))
    count, foreign_total = mycursor.fetchone()
    currency_name = get_currency_name(currency_id)
    foreign_total = float(foreign_total)
    twd_f = parse_positive_number(twd_total)
    if twd_f is None:
        return f"已選 {count} 筆｜待付合計：**{foreign_total:,.2f} {currency_name}**。請輸入實際匯出的台幣總額以試算匯率。"
    rate = twd_f / foreign_total if foreign_total else 0
    return (
        f"已選 {count} 筆｜外幣合計：**{foreign_total:,.2f} {currency_name}** ｜"
        f"實際匯出：**NT$ {twd_f:,.2f}** ｜實際匯率：**1 {currency_name} = NT$ {rate:,.4f}**"
    )


def insert_payable_payment(supplier_id, currency_id, pdate, twd_total, selected_ids, note):
    balance_display = get_supplier_balances()
    payment_display, payment_raw = get_payable_payments_list()
    empty_alloc = []

    if not supplier_id:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 請選擇廠商", gr.update(), gr.update(), empty_alloc, "請先選擇廠商。"
    if not currency_id:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 請選擇本次付款幣別", gr.update(), gr.update(), empty_alloc, "請選擇本次付款的幣別。"
    date_ok = parse_date_str(pdate)
    if date_ok is None:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 日期格式必須是 YYYY-MM-DD", gr.update(), gr.update(), empty_alloc, preview_payable_payment(supplier_id, currency_id, selected_ids, twd_total)
    twd_f = parse_positive_number(twd_total)
    if twd_f is None:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 台幣總額必須是有效的數字", gr.update(), gr.update(), empty_alloc, preview_payable_payment(supplier_id, currency_id, selected_ids, twd_total)
    if not selected_ids:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 請至少勾選一筆要涵蓋的進貨紀錄", gr.update(), gr.update(), empty_alloc, "請勾選至少一筆進貨紀錄。"

    placeholders = ",".join(["%s"] * len(selected_ids))
    mycursor.execute(
        f"""
        SELECT pu.id, pu.total_price
        FROM purchases pu
        WHERE pu.supplier_id=%s AND pu.currency_id=%s AND pu.id IN ({placeholders})
          AND NOT EXISTS (SELECT 1 FROM payable_payment_allocations ppa WHERE ppa.purchase_id=pu.id)
        """,
        (supplier_id, currency_id, *selected_ids),
    )
    rows = sorted(mycursor.fetchall(), key=lambda r: r[0])
    if len(rows) != len(selected_ids):
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 選取資料已變更，請重新選擇要付款的進貨紀錄", gr.update(), gr.update(), empty_alloc, "請重新選擇要付款的進貨紀錄。"
    foreign_total = sum(float(r[1]) for r in rows)
    if foreign_total <= 0:
        return gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, "⚠️ 選取的進貨金額加總異常", gr.update(), gr.update(), empty_alloc, "無法計算匯率。"

    effective_rate = twd_f / foreign_total

    mydb.begin()  # 付款與分配明細要一起寫入
    mycursor.execute(
        "INSERT INTO payable_payments (supplier_id, payment_date, amount, exchange_rate, note) VALUES (%s, %s, %s, %s, %s)",
        (supplier_id, date_ok, twd_f, round(effective_rate, 4), (note or "").strip() or None),
    )
    payment_id = mycursor.lastrowid

    allocated_sum = 0.0
    for i, (pid, original_amount) in enumerate(rows):
        if i == len(rows) - 1:
            twd_amount = round(twd_f - allocated_sum, 2)  # 最後一筆補齊差額，避免四捨五入誤差
        else:
            twd_amount = round(float(original_amount) * effective_rate, 2)
            allocated_sum += twd_amount
        mycursor.execute(
            "INSERT INTO payable_payment_allocations (payment_id, purchase_id, original_amount, twd_amount, effective_rate) VALUES (%s, %s, %s, %s, %s)",
            (payment_id, pid, float(original_amount), twd_amount, round(effective_rate, 4)),
        )
    mydb.commit()

    balance_display = get_supplier_balances()
    payment_display, payment_raw = get_payable_payments_list()
    msg = f"✅ 登記成功，這次反推出的實際匯率約為 {effective_rate:.4f}，共分配到 {len(rows)} 筆進貨"
    return (
        gr.update(value=balance_display), gr.update(value=payment_display), payment_raw, msg,
        gr.update(value=None), gr.update(choices=[], value=[]), empty_alloc,
        "付款已登記；請重新選擇下一次付款的廠商與幣別。"
    )


def delete_payable_payment(edit_id, supplier_id, currency_id):
    balance_display = get_supplier_balances()
    payment_display, payment_raw = get_payable_payments_list()
    if edit_id is None:
        return (
            gr.update(value=balance_display), gr.update(value=payment_display), payment_raw,
            "⚠️ 請先選取一筆紀錄再刪除", edit_id, [], gr.update(),
            preview_payable_payment(supplier_id, currency_id, [], "")
        )
    # payable_payment_allocations 設了 ON DELETE CASCADE，刪除付款會自動一併刪除分配明細，
    # 被涵蓋的那些進貨紀錄也會自動恢復成「尚未分配」狀態，可以重新勾選登記
    mycursor.execute("DELETE FROM payable_payments WHERE id=%s", (edit_id,))
    mydb.commit()
    balance_display = get_supplier_balances()
    payment_display, payment_raw = get_payable_payments_list()
    choices_update, hint = refresh_unallocated_choices(supplier_id, currency_id)
    return (
        gr.update(value=balance_display), gr.update(value=payment_display), payment_raw,
        "✅ 已刪除，相關進貨紀錄已恢復成尚未分配", None, [], choices_update, hint
    )


def refresh_payable_supplier_dropdown():
    return gr.update(choices=get_options("suppliers"))


# ========== 基礎資料 ==========

def get_basic_list(table):
    """取得基礎資料的 ID 與名稱，供清單顯示與選取使用。"""
    sql = "SELECT id, name FROM %s ORDER BY id" % table
    mycursor.execute(sql)
    return [list(row) for row in mycursor.fetchall()]


def insert_basic_data(table, name):
    name = (name or "").strip()
    if not name:
        return get_basic_list(table), "", "⚠️ 名稱不可空白，請輸入後再新增"
    sql = "INSERT INTO %s (name) VALUES (%%s)" % table
    mycursor.execute(sql, (name,))
    mydb.commit()
    return get_basic_list(table), "", f"✅ 新增成功：{name}"


def select_basic_row(evt: gr.SelectData, table_data):
    """將點選的 Dataframe 資料列帶入編輯欄位。"""
    row_idx = evt.index[0]
    # Gradio 的 Dataframe 輸入通常是 pandas DataFrame；[] 取的是欄而不是列。
    # 使用 iloc 才能依畫面上點選的列號正確取得 ID 與名稱。
    row = table_data.iloc[row_idx] if hasattr(table_data, "iloc") else table_data[row_idx]
    id_val, name_val = list(row)
    return id_val, name_val, f"已選取 ID {id_val}，可以修改或刪除"


def update_basic_data(table, edit_id, name):
    name = (name or "").strip()
    if edit_id is None:
        return get_basic_list(table), edit_id, name, "⚠️ 請先在下方清單點選一筆資料再修改"
    if not name:
        return get_basic_list(table), edit_id, name, "⚠️ 名稱不可空白"
    sql = "UPDATE %s SET name=%%s WHERE id=%%s" % table
    mycursor.execute(sql, (name, edit_id))
    mydb.commit()
    return get_basic_list(table), None, "", f"✅ 修改成功：ID {edit_id} → {name}"


def delete_basic_data(table, edit_id):
    if edit_id is None:
        return get_basic_list(table), edit_id, gr.update(), "⚠️ 請先在下方清單點選一筆資料再刪除"
    try:
        sql = "DELETE FROM %s WHERE id=%%s" % table
        mycursor.execute(sql, (edit_id,))
        mydb.commit()
        return get_basic_list(table), None, "", f"✅ 已刪除 ID {edit_id}"
    except pymysql.err.IntegrityError:
        mydb.rollback()
        return (
            get_basic_list(table), edit_id, gr.update(),
            "⚠️ 無法刪除，這筆資料已被進貨／出貨紀錄使用，請先刪除相關紀錄"
        )


def insert_supplier(name):
    data, cleared, msg = insert_basic_data("suppliers", name)
    return gr.update(value=data), cleared, msg


def update_supplier(edit_id, name):
    data, new_id, new_name, msg = update_basic_data("suppliers", edit_id, name)
    return gr.update(value=data), new_id, new_name, msg


def delete_supplier(edit_id):
    data, new_id, new_name, msg = delete_basic_data("suppliers", edit_id)
    return gr.update(value=data), new_id, new_name, msg


def insert_product(name):
    data, cleared, msg = insert_basic_data("products", name)
    return gr.update(value=data), cleared, msg


def update_product(edit_id, name):
    data, new_id, new_name, msg = update_basic_data("products", edit_id, name)
    return gr.update(value=data), new_id, new_name, msg


def delete_product(edit_id):
    data, new_id, new_name, msg = delete_basic_data("products", edit_id)
    return gr.update(value=data), new_id, new_name, msg


def insert_customer(name):
    data, cleared, msg = insert_basic_data("customers", name)
    return gr.update(value=data), cleared, msg


def update_customer(edit_id, name):
    data, new_id, new_name, msg = update_basic_data("customers", edit_id, name)
    return gr.update(value=data), new_id, new_name, msg


def delete_customer(edit_id):
    data, new_id, new_name, msg = delete_basic_data("customers", edit_id)
    return gr.update(value=data), new_id, new_name, msg


def insert_currency(name):
    data, cleared, msg = insert_basic_data("currency", name)
    return gr.update(value=data), cleared, msg


def update_currency(edit_id, name):
    data, new_id, new_name, msg = update_basic_data("currency", edit_id, name)
    return gr.update(value=data), new_id, new_name, msg


def delete_currency(edit_id):
    data, new_id, new_name, msg = delete_basic_data("currency", edit_id)
    return gr.update(value=data), new_id, new_name, msg


def refresh_purchase_dropdowns():
    """點進『進貨』分頁時，重新查一次廠商/貨品/幣別下拉選單，確保永遠是最新的"""
    return (
        gr.update(choices=get_options("suppliers")),
        gr.update(choices=get_options("products")),
        gr.update(choices=get_options("currency")),
    )


def refresh_sales_dropdowns():
    """點進『出貨』分頁時，重新查一次客戶/貨品下拉選單，確保永遠是最新的"""
    return (
        gr.update(choices=get_options("customers")),
        gr.update(choices=get_options("products")),
    )


def auto_calculate_total(qty_str, price_str):
    try:
        qty = float(qty_str) if qty_str else 0.0
        price = float(price_str) if price_str else 0.0
        total = qty * price
        if total == 0:
            return ""
        return str(int(total)) if total.is_integer() else f"{total:.2f}"
    except ValueError:
        return ""


# ========== 匯出 ==========

EXPORT_DIR = os.path.join(tempfile.gettempdir(), f"{APP_NAME}_erp_匯出")
ALL_MONTHS_LABEL = "全年"
MONEY_FORMAT = "#,##0.00"
QTY_FORMAT = "#,##0.##"

_thin = Side(style="thin", color="999999")
CELL_BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")


def get_export_year_choices():
    years = sorted({str(y) for y, m in get_inventory_year_months()}, reverse=True)
    return years or [str(datetime.now().year)]


def period_where(alias, year, month):
    """依年份＋月份（或『全年』）組出 WHERE 條件與參數"""
    if month == ALL_MONTHS_LABEL:
        return f"YEAR({alias}.transaction_time) = %s", [int(year)]
    return f"YEAR({alias}.transaction_time) = %s AND MONTH({alias}.transaction_time) = %s", [int(year), int(month)]


def period_label(year, month):
    return f"{year} 年" if month == ALL_MONTHS_LABEL else f"{year} 年 {int(month):02d} 月"


def new_export_path(prefix):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    return os.path.join(EXPORT_DIR, f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.xlsx")


def write_table(ws, start_row, headers, rows, money_cols=(), qty_cols=()):
    """從 start_row 開始寫一個有框線的表格，回傳下一個空白列的列號"""
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=start_row, column=c, value=h)
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.border = CELL_BORDER
        cell.alignment = Alignment(horizontal="center")
    for r, row in enumerate(rows, start=start_row + 1):
        for c, val in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.border = CELL_BORDER
            if c in money_cols:
                cell.number_format = MONEY_FORMAT
            elif c in qty_cols:
                cell.number_format = QTY_FORMAT
    return start_row + len(rows) + 1


def autofit_columns(ws, min_width=8, max_width=40):
    widths = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            # 中文字大約佔兩個英文字寬
            length = sum(2 if ord(ch) > 127 else 1 for ch in str(cell.value))
            widths[cell.column] = max(widths.get(cell.column, 0), length)
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = max(min_width, min(max_width, w + 2))


def write_title(ws, title, subtitle, ncols):
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=16)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    ws.cell(row=1, column=1).alignment = Alignment(horizontal="center")
    ws.cell(row=2, column=1, value=subtitle)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)


def fmt_date(val):
    return val.strftime("%Y-%m-%d") if hasattr(val, "strftime") else str(val)


def export_sales_excel(year, month, customer_id):
    where, params = period_where("sa", year, month)
    if customer_id:
        where += " AND sa.customer_id = %s"
        params.append(customer_id)
    mycursor.execute(f"""
        SELECT sa.transaction_time, c.name, p.name, sa.quantity, sa.unit, sa.unit_price, sa.total_price
        FROM sales sa
        LEFT JOIN customers c ON sa.customer_id = c.id
        LEFT JOIN products p ON sa.product_id = p.id
        WHERE {where}
        ORDER BY sa.transaction_time, sa.id
    """, params)
    rows = [[fmt_date(t), cname, pname, float(q), unit, float(up), float(tp)]
            for t, cname, pname, q, unit, up, tp in mycursor.fetchall()]
    if not rows:
        return None, f"⚠️ {period_label(year, month)}沒有符合條件的出貨紀錄"

    headers = ["日期", "客戶", "貨品", "數量", "單位", "單價", "總額"]
    wb = Workbook()
    ws = wb.active
    ws.title = "出貨表"
    write_title(ws, f"{APP_NAME} 出貨表", f"期間：{period_label(year, month)}", len(headers))
    next_row = write_table(ws, 4, headers, rows, money_cols=(6, 7), qty_cols=(4,))
    ws.cell(row=next_row, column=6, value="合計").font = Font(bold=True)
    total_cell = ws.cell(row=next_row, column=7, value=round(sum(r[6] for r in rows), 2))
    total_cell.font = Font(bold=True)
    total_cell.number_format = MONEY_FORMAT
    autofit_columns(ws)

    path = new_export_path(f"出貨表_{year}{'' if month == ALL_MONTHS_LABEL else month}")
    wb.save(path)
    return path, f"✅ 已產生出貨表，共 {len(rows)} 筆，請點下方檔案下載"


def export_purchases_excel(year, month, supplier_id):
    where, params = period_where("pu", year, month)
    if supplier_id:
        where += " AND pu.supplier_id = %s"
        params.append(supplier_id)
    mycursor.execute(f"""
        SELECT pu.transaction_time, s.name, p.name, pu.quantity, pu.unit, pu.unit_price, pu.total_price, cur.name
        FROM purchases pu
        LEFT JOIN suppliers s ON pu.supplier_id = s.id
        LEFT JOIN products p ON pu.product_id = p.id
        LEFT JOIN currency cur ON pu.currency_id = cur.id
        WHERE {where}
        ORDER BY pu.transaction_time, pu.id
    """, params)
    rows = [[fmt_date(t), sname, pname, float(q), unit, float(up), float(tp), cname]
            for t, sname, pname, q, unit, up, tp, cname in mycursor.fetchall()]
    if not rows:
        return None, f"⚠️ {period_label(year, month)}沒有符合條件的進貨紀錄"

    headers = ["日期", "廠商", "貨品", "數量", "單位", "單價", "總額", "幣別"]
    wb = Workbook()
    ws = wb.active
    ws.title = "進貨表"
    write_title(ws, f"{APP_NAME} 進貨表", f"期間：{period_label(year, month)}", len(headers))
    next_row = write_table(ws, 4, headers, rows, money_cols=(6, 7), qty_cols=(4,))

    # 不同幣別不能直接相加，所以依幣別分開小計
    subtotals = {}
    for r in rows:
        subtotals[r[7]] = subtotals.get(r[7], 0.0) + r[6]
    for cname, amt in sorted(subtotals.items(), key=lambda kv: str(kv[0])):
        ws.cell(row=next_row, column=6, value="合計").font = Font(bold=True)
        amt_cell = ws.cell(row=next_row, column=7, value=round(amt, 2))
        amt_cell.font = Font(bold=True)
        amt_cell.number_format = MONEY_FORMAT
        ws.cell(row=next_row, column=8, value=cname).font = Font(bold=True)
        next_row += 1
    autofit_columns(ws)

    path = new_export_path(f"進貨表_{year}{'' if month == ALL_MONTHS_LABEL else month}")
    wb.save(path)
    return path, f"✅ 已產生進貨表，共 {len(rows)} 筆，請點下方檔案下載"


def export_invoice_excel(customer_id, year, month):
    """請款單：某客戶某月的出貨明細，加上前期未收、本期已收，算出本期應收總額"""
    if not customer_id:
        return None, "⚠️ 請選擇要請款的客戶"
    if month == ALL_MONTHS_LABEL:
        return None, "⚠️ 請款單請選擇單一月份"

    start = f"{int(year)}-{int(month):02d}-01"
    end = get_month_cutoff(year, month)

    mycursor.execute("SELECT name FROM customers WHERE id=%s", (customer_id,))
    row = mycursor.fetchone()
    customer_name = row[0] if row else f"客戶 #{customer_id}"

    mycursor.execute("""
        SELECT sa.transaction_time, p.name, sa.quantity, sa.unit, sa.unit_price, sa.total_price
        FROM sales sa
        LEFT JOIN products p ON sa.product_id = p.id
        WHERE sa.customer_id = %s AND sa.transaction_time >= %s AND sa.transaction_time < %s
        ORDER BY sa.transaction_time, sa.id
    """, (customer_id, start, end))
    items = [[fmt_date(t), pname, float(q), unit, float(up), float(tp)]
             for t, pname, q, unit, up, tp in mycursor.fetchall()]

    mycursor.execute("SELECT COALESCE(SUM(total_price), 0) FROM sales WHERE customer_id=%s AND transaction_time < %s",
                     (customer_id, start))
    prev_sales = float(mycursor.fetchone()[0])
    mycursor.execute("SELECT COALESCE(SUM(amount), 0) FROM receivable_payments WHERE customer_id=%s AND payment_date < %s",
                     (customer_id, start))
    prev_paid = float(mycursor.fetchone()[0])
    mycursor.execute("""SELECT COALESCE(SUM(amount), 0) FROM receivable_payments
                        WHERE customer_id=%s AND payment_date >= %s AND payment_date < %s""",
                     (customer_id, start, end))
    period_paid = float(mycursor.fetchone()[0])

    prev_balance = round(prev_sales - prev_paid, 2)
    period_total = round(sum(r[5] for r in items), 2)
    amount_due = round(prev_balance + period_total - period_paid, 2)

    if not items and prev_balance == 0:
        return None, f"⚠️ {customer_name} 在 {period_label(year, month)}沒有出貨，也沒有前期未收款，不需要請款"

    headers = ["日期", "貨品", "數量", "單位", "單價", "金額"]
    wb = Workbook()
    ws = wb.active
    ws.title = "請款單"
    write_title(ws, f"{APP_NAME} 請款單", "", len(headers))
    ws.cell(row=2, column=1, value=f"客戶：{customer_name}")
    ws.cell(row=3, column=1, value=f"請款期間：{period_label(year, month)}")
    ws.cell(row=3, column=5, value=f"開立日期：{datetime.now():%Y-%m-%d}")

    next_row = write_table(ws, 5, headers, items, money_cols=(5, 6), qty_cols=(3,))
    next_row += 1
    summary = [
        ("前期未收", prev_balance),
        ("本期出貨合計", period_total),
        ("本期已收", -period_paid),
        ("本期應收總額", amount_due) if amount_due >= 0 else ("溢收金額（可抵下期）", -amount_due),
    ]
    for i, (label, val) in enumerate(summary):
        is_last = i == len(summary) - 1
        label_cell = ws.cell(row=next_row, column=5, value=label)
        val_cell = ws.cell(row=next_row, column=6, value=val)
        val_cell.number_format = MONEY_FORMAT
        for cell in (label_cell, val_cell):
            cell.border = CELL_BORDER
            if is_last:
                cell.font = Font(bold=True, size=12)
        next_row += 1
    autofit_columns(ws)
    ws.column_dimensions["A"].width = 14

    safe_name = "".join(ch for ch in customer_name if ch not in '\\/:*?"<>|')
    path = new_export_path(f"請款單_{safe_name}_{year}{int(month):02d}")
    wb.save(path)
    due_text = f"本期應收總額 {amount_due:,.2f}" if amount_due >= 0 else f"溢收 {-amount_due:,.2f}"
    return path, f"✅ 已產生 {customer_name} 的請款單，{due_text}，請點下方檔案下載"


def refresh_export_tab():
    """點進『匯出/匯入』分頁時，重新查一次年份與客戶/廠商選單"""
    years = get_export_year_choices()
    customers = get_options("customers")
    return (
        gr.update(choices=years),
        gr.update(choices=years),
        gr.update(choices=years),
        gr.update(choices=customers),
        gr.update(choices=[("全部客戶", 0)] + customers),
        gr.update(choices=[("全部廠商", 0)] + get_options("suppliers")),
    )


# ========== 匯入 ==========

UNIT_CHOICES = ["公斤", "隻"]
IMPORT_TYPES = {
    "出貨": ["日期", "客戶", "貨品", "數量", "單位", "單價", "總額"],
    "進貨": ["日期", "廠商", "貨品", "數量", "單位", "單價", "總額", "幣別"],
    "廠商": ["名稱"],
    "貨品": ["名稱"],
    "顧客": ["名稱"],
}
IMPORT_EXAMPLES = {
    "出貨": ["2026-09-01", "（請填基礎資料中已有的客戶名稱）", "（已有的貨品名稱）", 10, "公斤", 350, ""],
    "進貨": ["2026-09-01", "（已有的廠商名稱）", "（已有的貨品名稱）", 100, "公斤", 5.5, "", "（已有的幣別名稱）"],
    "廠商": ["（新廠商名稱）"],
    "貨品": ["（新貨品名稱）"],
    "顧客": ["（新顧客名稱）"],
}
BASIC_TABLE_OF = {"廠商": "suppliers", "貨品": "products", "顧客": "customers"}


def download_import_template(import_type):
    headers = IMPORT_TYPES[import_type]
    wb = Workbook()
    ws = wb.active
    ws.title = import_type
    write_table(ws, 1, headers, [IMPORT_EXAMPLES[import_type]])
    note_row = 4
    ws.cell(row=note_row, column=1, value="說明：第 1 列是欄位名稱請勿修改，第 2 列是範例請刪除後再填入資料。").font = Font(color="C00000")
    if import_type in ("出貨", "進貨"):
        ws.cell(row=note_row + 1, column=1, value="日期格式 YYYY-MM-DD，空白代表今天；單位只能填「公斤」或「隻」；總額空白會自動用 數量×單價 計算。").font = Font(color="C00000")
    autofit_columns(ws)
    path = new_export_path(f"匯入範本_{import_type}")
    wb.save(path)
    return path


def read_import_file(path):
    """讀取使用者上傳的 Excel / CSV，全部當成文字/原始值讀進來，後面再逐欄檢查"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, dtype=object)
    if ext == ".csv":
        # Excel 存出來的 CSV 在台灣常是 Big5(cp950)，UTF-8 讀失敗就換 cp950
        for enc in ("utf-8-sig", "cp950"):
            try:
                return pd.read_csv(path, dtype=object, encoding=enc)
            except UnicodeDecodeError:
                continue
        raise ValueError("無法辨識 CSV 的文字編碼，請另存成 UTF-8 或 Excel 檔")
    raise ValueError("只支援 .xlsx 或 .csv 檔案")


def cell_text(val):
    if val is None or (isinstance(val, float) and pd.isna(val)) or val is pd.NaT:
        return ""
    return str(val).strip()


def parse_import_date(val):
    """回傳 datetime 字串；空白代表現在時間；格式錯誤回傳 None"""
    if hasattr(val, "strftime") and not pd.isna(val):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    text = cell_text(val)
    if not text:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def name_to_id_map(table):
    return {name.strip(): id_ for name, id_ in get_options(table)}


TABLE_LABELS = {"customers": "客戶", "suppliers": "廠商", "products": "貨品"}


def normalize_name(name):
    """把名稱轉成比對用的形式：全形轉半形、去掉空格與括號等符號、英文轉小寫。
    例如「紅蟳(200g)」、「紅蟳 200G」、「紅蟳（200g）」都會變成「紅蟳200g」"""
    name = unicodedata.normalize("NFKC", name)
    return re.sub(r"[\s()\[\]{}\-_.,/·、]", "", name).lower()


def find_similar_names(new_names, existing_names):
    """把新名稱跟既有名稱、以及同一批其他新名稱比對，比對形式相同的歸成一組。
    只回傳至少包含一個新名稱、且超過一個名稱的組，例如 [[("現有", "紅蟳(200g)"), ("檔案中", "紅蟳200g")]]"""
    groups = {}
    for n in existing_names:
        groups.setdefault(normalize_name(n), []).append(("現有", n))
    for n in new_names:
        groups.setdefault(normalize_name(n), []).append(("檔案中", n))
    return [g for g in groups.values() if len(g) > 1 and any(src == "檔案中" for src, _ in g)]


def warning_box(html):
    """橘色底框，讓需要注意的訊息跟一般訊息區分開"""
    return ('<div style="border-left:5px solid #f59e0b;background:rgba(245,158,11,0.15);'
            f'padding:12px 16px;border-radius:6px;margin:8px 0;">{html}</div>')


def validate_import(file_path, import_type):
    """檢查上傳檔案的每一列，回傳 (預覽表格, 可匯入的資料, 訊息)。有任何一列錯誤就整批不匯入"""
    empty = pd.DataFrame(columns=["列號", "狀態"] + IMPORT_TYPES[import_type])
    if not file_path:
        return empty, [], "⚠️ 請先上傳檔案"
    try:
        df = read_import_file(file_path)
    except Exception as e:
        return empty, [], f"⚠️ 讀取檔案失敗：{e}"

    df.columns = [str(c).strip() for c in df.columns]
    required = IMPORT_TYPES[import_type]
    optional = {"總額", "日期"}
    missing = [c for c in required if c not in df.columns and c not in optional]
    if missing:
        return empty, [], f"⚠️ 檔案缺少欄位：{'、'.join(missing)}（可以先下載範本再填寫）"
    for c in required:
        if c not in df.columns:
            df[c] = None
    # 整列都空白的直接略過
    df = df[required].dropna(how="all")
    if df.empty:
        return empty, [], "⚠️ 檔案中沒有任何資料列"

    preview, valid_rows, error_count = [], [], 0
    # 出貨/進貨檔案中出現、但基礎資料還沒有的名稱：{資料表: [名稱, ...]}，確認匯入時會先新增
    new_names = {}

    if import_type in BASIC_TABLE_OF:
        name_cols = [(BASIC_TABLE_OF[import_type], 2)]  # (資料表, 名稱在預覽列中的位置)，給相似名稱標示用
        existing = set(name_to_id_map(BASIC_TABLE_OF[import_type]))
        seen = set()
        for idx, r in df.iterrows():
            name = cell_text(r["名稱"])
            excel_row = idx + 2  # 第 1 列是標題
            if not name:
                status = "❌ 名稱空白"
                error_count += 1
            elif name in existing:
                status = "⏭️ 已存在，略過"
            elif name in seen:
                status = "⏭️ 檔案內重複，略過"
            else:
                status = "✅ 可匯入"
                seen.add(name)
                valid_rows.append([name])
            preview.append([excel_row, status, name])
    else:
        party_label = "客戶" if import_type == "出貨" else "廠商"
        party_table = "customers" if import_type == "出貨" else "suppliers"
        party_map = name_to_id_map(party_table)
        product_map = name_to_id_map("products")
        currency_map = name_to_id_map("currency") if import_type == "進貨" else {}
        new_names = {party_table: [], "products": []}
        name_cols = [(party_table, 3), ("products", 4)]

        for idx, r in df.iterrows():
            excel_row = idx + 2
            errors = []
            tdate = parse_import_date(r["日期"])
            if tdate is None:
                errors.append("日期格式錯誤")
            for label, table, name_map in ((party_label, party_table, party_map), ("貨品", "products", product_map)):
                name = cell_text(r[label])
                if not name:
                    errors.append(f"{label}空白")
                elif name not in name_map and name not in new_names[table]:
                    new_names[table].append(name)
            party_name = cell_text(r[party_label])
            product_name = cell_text(r["貨品"])
            qty_f = parse_positive_number(cell_text(r["數量"]).replace(",", ""))
            if qty_f is None:
                errors.append("數量錯誤")
            price_f = parse_positive_number(cell_text(r["單價"]).replace(",", ""))
            if price_f is None:
                errors.append("單價錯誤")
            unit = cell_text(r["單位"])
            if unit not in UNIT_CHOICES:
                errors.append("單位需為公斤或隻")
            total_text = cell_text(r["總額"]).replace(",", "")
            total_f = parse_positive_number(total_text) if total_text else None
            if total_text and total_f is None:
                errors.append("總額錯誤")
            if total_f is None and qty_f is not None and price_f is not None:
                total_f = round(qty_f * price_f, 2)
            currency_name = ""
            if import_type == "進貨":
                currency_name = cell_text(r["幣別"])
                # 幣別會影響匯率與應付帳款計算，不自動新增，必須先到基礎資料設定建立
                if currency_name not in currency_map:
                    errors.append(f"找不到幣別「{currency_name}」，請先到基礎資料設定新增" if currency_name else "幣別空白")

            display = [excel_row, "", (tdate or cell_text(r["日期"]))[:10], party_name, product_name,
                       qty_f if qty_f is not None else cell_text(r["數量"]), unit,
                       price_f if price_f is not None else cell_text(r["單價"]),
                       total_f if total_f is not None else total_text]
            if import_type == "進貨":
                display.append(currency_name)

            if errors:
                display[1] = "❌ " + "、".join(errors)
                error_count += 1
            else:
                # 客戶/廠商、貨品先存名稱，確認匯入時新增完缺少的基礎資料後再換成 id
                rec = [tdate, party_name, product_name, qty_f, unit, price_f, total_f]
                if import_type == "進貨":
                    rec.append(currency_map[currency_name])
                valid_rows.append(rec)
            preview.append(display)

    new_names = {t: names for t, names in new_names.items() if names}

    # 相似名稱檢查：匯入廠商/貨品/顧客時，要新增的就是檔案裡的名稱本身；出貨/進貨則是自動新增的名稱
    names_to_check = {BASIC_TABLE_OF[import_type]: [r[0] for r in valid_rows]} if import_type in BASIC_TABLE_OF else new_names
    similar_lines, warn_by_name = [], {}
    for table, names in names_to_check.items():
        for group in find_similar_names(names, list(name_to_id_map(table))):
            similar_lines.append(f"<li>{TABLE_LABELS[table]}：" + " ↔ ".join(f"{src}的「{n}」" for src, n in group) + "</li>")
            for src, n in group:
                if src == "檔案中":
                    others = "、".join(f"{s}的「{o}」" for s, o in group if (s, o) != (src, n))
                    warn_by_name[(table, n)] = f"⚠️ {TABLE_LABELS[table]}「{n}」跟{others}幾乎一樣"
    # 把相似名稱的提醒也標在表格的每一列上，讓人在表格就看得到
    for p in preview:
        warns = [warn_by_name[(t, p[i])] for t, i in name_cols if (t, p[i]) in warn_by_name]
        if warns and not p[1].startswith("⏭️"):
            p[1] = "；".join(([p[1]] if p[1].startswith("❌") else []) + warns)

    # 表格只列出有錯誤或需要注意的列，都沒問題時表格是空的
    flagged_df = pd.DataFrame([p for p in preview if p[1].startswith(("❌", "⚠️"))], columns=["列號", "狀態"] + required)
    if error_count:
        return flagged_df, [], f"⚠️ 有 {error_count} 列資料有問題（見下方表格），請修正檔案後重新上傳檢查。為避免只匯入一半，這次不會匯入任何資料。"
    if not valid_rows:
        return flagged_df, [], "ℹ️ 沒有需要匯入的新資料"

    if similar_lines:
        msg = f"### ⚠️ 有 {len(similar_lines)} 組名稱疑似打錯字，請先確認再匯入（共 {len(valid_rows)} 筆資料）"
        msg += "\n\n" + warning_box(
            "<b>以下新名稱跟其他名稱幾乎一樣（只差空格、括號或全形半形）：</b>"
            f"<ul>{''.join(similar_lines)}</ul>"
            "如果是同一個，請把檔案裡的名稱改成一致後重新上傳檢查；確定是不同的就直接匯入。"
            "用到這些名稱的資料列標示在下方表格。"
        )
    else:
        msg = f"### ✅ 檢查通過，共 {len(valid_rows)} 筆可匯入"
    if new_names:
        summary = "、".join(f"{TABLE_LABELS[t]} {len(n)} 個" for t, n in new_names.items())
        details = "\n".join(f"<p>{TABLE_LABELS[t]}：{'、'.join(n)}</p>" for t, n in new_names.items())
        msg += (f"\n\n🆕 匯入時會自動新增：{summary}"
                f"\n\n<details><summary>點這裡查看完整名單</summary>{details}</details>")
    msg += "\n\n確認無誤請按「確認匯入」"
    return flagged_df, {"rows": valid_rows, "new": new_names}, msg


def confirm_import(import_type, state):
    if not state or not state.get("rows"):
        return "⚠️ 沒有可匯入的資料，請先上傳檔案並按「檢查資料」", state
    rows, new_names = state["rows"], state["new"]
    try:
        # 先新增缺少的客戶/廠商/貨品，跟出貨/進貨在同一個交易裡，失敗就全部復原
        mydb.begin()
        for table, names in new_names.items():
            mycursor.executemany("INSERT INTO %s (name) VALUES (%%s)" % table, [[n] for n in names])

        if import_type in BASIC_TABLE_OF:
            mycursor.executemany("INSERT INTO %s (name) VALUES (%%s)" % BASIC_TABLE_OF[import_type], rows)
        else:
            party_table = "customers" if import_type == "出貨" else "suppliers"
            party_map = name_to_id_map(party_table)
            product_map = name_to_id_map("products")
            db_rows = [[r[0], party_map[r[1]], product_map[r[2]], *r[3:]] for r in rows]
            if import_type == "出貨":
                mycursor.executemany("""
                    INSERT INTO sales (transaction_time, customer_id, product_id, quantity, unit, unit_price, total_price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, db_rows)
            else:
                mycursor.executemany("""
                    INSERT INTO purchases (transaction_time, supplier_id, product_id, quantity, unit, unit_price, total_price, currency_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, db_rows)
        mydb.commit()
    except (pymysql.MySQLError, KeyError) as e:
        mydb.rollback()
        return f"⚠️ 匯入失敗，已全部復原，沒有寫入任何資料：{e}", state
    msg = f"✅ 已成功匯入 {len(rows)} 筆{import_type}資料"
    added = sum(len(n) for n in new_names.values())
    if added:
        msg += f"，並新增 {added} 筆基礎資料"
    return msg, []


def reset_import_preview(import_type):
    return pd.DataFrame(columns=["列號", "狀態"] + IMPORT_TYPES[import_type]), [], ""


# ========== 備份/還原 ==========
# 備份檔是純文字的 .sql：每張資料表一段 DROP / CREATE，之後每筆資料一行 INSERT。
# 不用 mysqldump，因為它在 Windows 上處理中文資料庫名稱會出錯。

RESTORE_CONFIRM_WORD = "還原"
_SAFE_STATEMENT = re.compile(
    r"^(DROP TABLE IF EXISTS `[^`]+`;"
    r"|CREATE TABLE `[^`]+` \("
    r"|INSERT INTO `[^`]+` (\(|VALUES \()"  # 有欄位清單的是新格式，VALUES 直接接資料的是早期手動備份的格式
    r"|SET FOREIGN_KEY_CHECKS=[01];"
    r"|SET NAMES utf8mb4;)"
)
_INSERT_TABLE = re.compile(r"^INSERT INTO `([^`]+)`")
_CREATE_TABLE = re.compile(r"^CREATE TABLE `([^`]+)`")


def new_backup_path(kind="備份"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return os.path.join(BACKUP_DIR, f"{DB_NAME}_{kind}_{datetime.now():%Y%m%d_%H%M%S}.sql")


def dump_database(conn, path):
    """把整個資料庫寫成 .sql 檔，回傳 {資料表: 筆數}。
    用一致性快照讀取，備份途中就算有人新增資料，各張表的內容也會是同一個時間點"""
    cur = conn.cursor()
    counts = {}
    cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
    try:
        cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        tables = [row[0] for row in cur.fetchall()]
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"-- ERP-BACKUP v1\n-- 資料庫：{DB_NAME}\n-- 時間：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write("SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n\n")
            for t in tables:
                cur.execute(f"SHOW CREATE TABLE `{t}`")
                f.write(f"DROP TABLE IF EXISTS `{t}`;\n{cur.fetchone()[1]};\n")
                cur.execute(f"SELECT * FROM `{t}`")
                cols = ", ".join(f"`{d[0]}`" for d in cur.description)
                n = 0
                for row in cur.fetchall():
                    f.write(f"INSERT INTO `{t}` ({cols}) VALUES ({', '.join(conn.literal(v) for v in row)});\n")
                    n += 1
                counts[t] = n
                f.write("\n")
            f.write("SET FOREIGN_KEY_CHECKS=1;\n")
    finally:
        cur.execute("COMMIT")
    return counts


def parse_backup_file(path):
    """讀取備份檔，切成一句一句的 SQL。每句都必須是備份檔會出現的那幾種，不符合就整個檔案拒絕，
    避免有人上傳夾帶其他指令的檔案。回傳 (SQL 清單, {資料表: 檔內筆數})"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    statements, buf = [], []
    for line in text.split("\n"):
        if not buf and (not line.strip() or line.startswith("--")):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        raise ValueError("檔案結尾不完整，可能下載或複製到一半")
    counts, has_create = {}, False
    for st in statements:
        if not _SAFE_STATEMENT.match(st):
            raise ValueError(f"檔案中有不認得的指令，已拒絕：{st[:60]}")
        m = _CREATE_TABLE.match(st)
        if m:
            has_create = True
            counts.setdefault(m.group(1), 0)
        m = _INSERT_TABLE.match(st)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    if not has_create:
        raise ValueError("這不是 ERP 的備份檔（找不到任何資料表）")
    return statements, counts


def restore_database(conn, path):
    """用備份檔覆蓋資料庫。DROP/CREATE 會讓 MySQL 自動提交，所以無法整個復原；
    呼叫前務必先做好備份。回傳還原後 {資料表: 筆數}"""
    statements, _ = parse_backup_file(path)
    cur = conn.cursor()
    conn.autocommit(False)  # 資料的 INSERT 集中提交，比一筆筆提交快很多
    try:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for st in statements:
            cur.execute(st)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.autocommit(True)
    return count_rows(conn)


def count_rows(conn):
    cur = conn.cursor()
    cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
    counts = {}
    for t, _type in cur.fetchall():
        cur.execute(f"SELECT COUNT(*) FROM `{t}`")
        counts[t] = cur.fetchone()[0]
    return counts


def list_backups():
    """備份資料夾裡的 .sql 檔，由新到舊：[(檔名, 大小KB, 時間), ...]"""
    if not os.path.isdir(BACKUP_DIR):
        return []
    files = [os.path.join(BACKUP_DIR, n) for n in os.listdir(BACKUP_DIR) if n.lower().endswith(".sql")]
    files.sort(key=os.path.getmtime, reverse=True)
    return [(os.path.basename(p), round(os.path.getsize(p) / 1024, 1),
             datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")) for p in files]


def backup_list_updates():
    items = list_backups()
    return gr.update(value=[list(i) for i in items]), gr.update(choices=[i[0] for i in items])


def do_backup():
    path = new_backup_path()
    try:
        counts = dump_database(_get_db_connection(), path)
    except Exception as e:
        if os.path.exists(path):
            os.remove(path)
        return None, f"⚠️ 備份失敗：{e}", *backup_list_updates()
    total = sum(counts.values())
    msg = f"✅ 備份完成：共 {len(counts)} 張資料表、{total:,} 筆資料\n\n存放位置：`{path}`\n\n也可以點下方檔案直接下載一份。"
    return path, msg, *backup_list_updates()


def resolve_restore_source(uploaded, chosen):
    """上傳的檔案優先，沒有才用下拉選單選的備份"""
    if uploaded:
        return uploaded
    if chosen:
        path = os.path.join(BACKUP_DIR, os.path.basename(chosen))  # basename 避免跑到備份資料夾以外
        if os.path.isfile(path):
            return path
    return None


def inspect_backup(uploaded, chosen):
    """還原前先看備份檔的內容，跟目前資料庫比對筆數，讓人確認是不是選對檔案"""
    path = resolve_restore_source(uploaded, chosen)
    if not path:
        return pd.DataFrame(columns=["資料表", "備份檔筆數", "目前筆數"]), None, "⚠️ 請先上傳備份檔，或從下拉選單選一個備份"
    try:
        _, file_counts = parse_backup_file(path)
    except Exception as e:
        return pd.DataFrame(columns=["資料表", "備份檔筆數", "目前筆數"]), None, f"⚠️ 這個檔案不能用來還原：{e}"
    current = count_rows(_get_db_connection())
    rows = [[t, file_counts.get(t, "（備份檔沒有，不會動）"), current.get(t, "（目前沒有）")]
            for t in sorted(set(file_counts) | set(current))]
    msg = warning_box(
        f"<b>即將用「{os.path.basename(path)}」覆蓋目前的資料。</b><br>"
        "備份檔裡有的資料表，目前的內容會<b>全部被取代</b>；備份之後新增的資料會消失。<br>"
        "還原前系統會先自動備份一份目前的資料，萬一還原錯了還能救回來。<br>"
        f"確認要還原，請在下方輸入「{RESTORE_CONFIRM_WORD}」兩個字，再按「確認還原」。"
    )
    return pd.DataFrame(rows, columns=["資料表", "備份檔筆數", "目前筆數"]), path, msg


def do_restore(path, confirm_text):
    if not path:
        return "⚠️ 請先按「檢查備份檔」，確認要還原的內容", gr.update(), gr.update(), gr.update()
    if (confirm_text or "").strip() != RESTORE_CONFIRM_WORD:
        return f"⚠️ 請在確認欄輸入「{RESTORE_CONFIRM_WORD}」兩個字", gr.update(), gr.update(), gr.update()

    conn = _get_db_connection()
    safety_path = new_backup_path("還原前自動備份")
    try:
        dump_database(conn, safety_path)
    except Exception as e:
        return f"⚠️ 還原前的自動備份失敗，為了安全沒有還原：{e}", gr.update(), gr.update(), gr.update()

    try:
        after = restore_database(conn, path)
    except Exception as e:
        # 還原到一半失敗：資料可能是半新半舊，馬上用剛做的自動備份復原
        try:
            restore_database(conn, safety_path)
            fixed = "已自動用還原前的備份復原，資料跟還原前一樣。"
        except Exception as e2:
            fixed = f"**自動復原也失敗（{e2}）**，請手動用這個檔案還原：`{safety_path}`"
        return f"⚠️ 還原失敗：{e}\n\n{fixed}", *backup_list_updates(), gr.update(value="")
    _, file_counts = parse_backup_file(path)
    mismatch = [t for t, n in file_counts.items() if after.get(t) != n]
    msg = (f"✅ 還原完成，共 {len(file_counts)} 張資料表。\n\n還原前的資料已自動備份：`{safety_path}`\n\n"
           "**請重新整理瀏覽器頁面**，各分頁的選單才會更新。")
    if mismatch:
        msg += f"\n\n⚠️ 這幾張表的筆數跟備份檔不一致，請確認：{'、'.join(mismatch)}"
    return msg, *backup_list_updates(), gr.update(value="")


with gr.Blocks() as demo:
    with gr.Tabs() as tabs:
        with gr.TabItem("首頁", id="home"):
            gr.Markdown(
                f"## 歡迎使用{APP_NAME} ERP\n\n"
                "請從上方分頁選擇「出貨」、「進貨」、「庫存」或「基礎資料設定」開始操作。\n\n"
                "廠商／貨品／顧客／幣別的新增、修改、刪除，都可在本頁面的「基礎資料設定」分頁完成。\n\n"
                "請款單、出貨表、進貨表的 Excel 匯出，以及批次匯入資料，請到「匯出/匯入」分頁。"
            )

        with gr.TabItem("出貨", id="sales") as sales_tab:
            sale_edit_id = gr.State(value=None)
            with gr.Row():
                cus_dp = gr.Dropdown(choices=get_options("customers"), label="客戶", interactive=True)
                pro_dp2 = gr.Dropdown(choices=get_options("products"), label="貨品", interactive=True)
                qua2 = gr.Textbox(label="數量")
                unit_pri2 = gr.Textbox(label="單價")
                total2 = gr.Textbox(label="總額(可手動修改)")
                unit_dp2 = gr.Dropdown(choices=["公斤", "隻"], value="公斤", label="計量單位", interactive=True)
            with gr.Row():
                insert2 = gr.Button("新增")
                sale_update_btn = gr.Button("修改所選")
                sale_delete_btn = gr.Button("刪除所選", variant="stop")
            sale_msg = gr.Markdown("")

            gr.Markdown("<h3 style='margin-top: 40px;'>出貨紀錄（點一列可選取來修改或刪除）</h3>")
            with gr.Row():
                year_months_sales = get_available_year_months_sales()

                if year_months_sales:
                    latest_year_s, latest_month_s = year_months_sales[0]
                else:
                    now = datetime.now()
                    latest_year_s, latest_month_s = now.year, now.month

                available_years_s = sorted({str(y) for y, m in year_months_sales}, reverse=True)
                if not available_years_s:
                    available_years_s = [str(latest_year_s)]

                year_dp2 = gr.Dropdown(
                    choices=available_years_s,
                    value=str(latest_year_s),
                    label="年份",
                    interactive=True,
                )
                month_dp2 = gr.Dropdown(
                    choices=[f"{m:02d}" for m in range(1, 13)],
                    value=f"{latest_month_s:02d}",
                    label="月份",
                    interactive=True,
                )
                refresh_btn2 = gr.Button("查詢")

            sales_table = gr.Dataframe(
                value=get_sales_table(str(latest_year_s), f"{latest_month_s:02d}"),
                headers=["時間", "客戶", "貨品", "數量", "單位", "單價", "總額"],
                row_count=(1, "dynamic"),
                interactive=False,
            )
            sales_raw_state = gr.State(value=get_sales_raw(str(latest_year_s), f"{latest_month_s:02d}"))

        with gr.TabItem("進貨", id="purchases") as purchases_tab:
            purchase_edit_id = gr.State(value=None)
            with gr.Row():
                sup_dp = gr.Dropdown(choices=get_options("suppliers"), label="廠商", interactive=True)
                pro_dp = gr.Dropdown(choices=get_options("products"), label="貨品", interactive=True)
                qua = gr.Textbox(label="數量")
                unit_pri = gr.Textbox(label="單價")
                total = gr.Textbox(label="總額(可手動修改)")
                # 新增商品或交易時，單位寫死常用選項，簡單又不會出錯
                unit_dp = gr.Dropdown(choices=["公斤", "隻"], value="公斤", label="計量單位", interactive=True)
                Cur_dp = gr.Dropdown(choices=get_options("currency"), label="幣別", interactive=True)
            with gr.Row():
                insert = gr.Button("新增")
                purchase_update_btn = gr.Button("修改所選")
                purchase_delete_btn = gr.Button("刪除所選", variant="stop")
            purchase_msg = gr.Markdown("")

            gr.Markdown("<h3 style='margin-top: 40px;'>進貨紀錄（點一列可選取來修改或刪除）</h3>")
            with gr.Row():
                year_months = get_available_year_months()

                if year_months:
                    latest_year, latest_month = year_months[0]
                else:
                    now = datetime.now()
                    latest_year, latest_month = now.year, now.month

                available_years = sorted({str(y) for y, m in year_months}, reverse=True)
                if not available_years:
                    available_years = [str(latest_year)]

                year_dp = gr.Dropdown(
                    choices=available_years,
                    value=str(latest_year),
                    label="年份",
                    interactive=True,
                )
                month_dp = gr.Dropdown(
                    choices=[f"{m:02d}" for m in range(1, 13)],
                    value=f"{latest_month:02d}",
                    label="月份",
                    interactive=True,
                )
                refresh_btn = gr.Button("查詢")

            purchases_table = gr.Dataframe(
                value=get_purchases_table(str(latest_year), f"{latest_month:02d}"),
                headers=["時間", "廠商", "貨品", "數量", "單位", "單價", "總額", "幣別"],
                row_count=(1, "dynamic"),
                interactive=False,
            )
            purchases_raw_state = gr.State(value=get_purchases_raw(str(latest_year), f"{latest_month:02d}"))

        with gr.TabItem("庫存", id="inventory") as inventory_tab:
            gr.Markdown(
                "理論庫存 = 從系統最早的進貨/出貨紀錄開始，累加到所選月份月底為止。\n\n"
                "點清單裡的一列可以帶入下方表單，登記或更新該貨品這個月的實際盤點數量；"
                "沒有登記實際盤點的貨品，不會計算死傷。"
            )
            with gr.Row():
                inv_year_months = get_inventory_year_months()
                if inv_year_months:
                    inv_latest_year, inv_latest_month = inv_year_months[0]
                else:
                    now = datetime.now()
                    inv_latest_year, inv_latest_month = now.year, now.month

                inv_available_years = sorted({str(y) for y, m in inv_year_months}, reverse=True)
                if not inv_available_years:
                    inv_available_years = [str(inv_latest_year)]

                inv_year_dp = gr.Dropdown(
                    choices=inv_available_years,
                    value=str(inv_latest_year),
                    label="年份",
                    interactive=True,
                )
                inv_month_dp = gr.Dropdown(
                    choices=[f"{m:02d}" for m in range(1, 13)],
                    value=f"{inv_latest_month:02d}",
                    label="月份",
                    interactive=True,
                )
                inv_refresh_btn = gr.Button("查詢")

            inv_display_init, inv_raw_init = get_inventory_table(str(inv_latest_year), f"{inv_latest_month:02d}")
            inventory_table = gr.Dataframe(
                value=inv_display_init,
                headers=["貨品", "單位", "理論庫存", "實際盤點", "死傷（理論-實際）"],
                row_count=(1, "dynamic"),
                interactive=False,
            )
            inventory_raw_state = gr.State(value=inv_raw_init)
            inventory_selected_state = gr.State(value=None)

            gr.Markdown("<h4 style='margin-top: 30px;'>登記 / 更新實際盤點</h4>")
            with gr.Row():
                inv_pro_dp = gr.Dropdown(choices=get_options("products"), label="貨品", interactive=True)
                inv_unit_dp = gr.Dropdown(choices=["公斤", "隻"], label="單位", interactive=True)
                inv_actual_qty = gr.Textbox(label="實際盤點數量")
            with gr.Row():
                inv_save_btn = gr.Button("登記盤點")
                inv_clear_btn = gr.Button("清除此筆盤點", variant="stop")
            inv_msg = gr.Markdown("")

        with gr.TabItem("基礎資料設定", id="basic-data"):
            gr.Markdown("管理廠商、貨品、顧客與幣別。點選清單的一列後，可修改或刪除該筆資料。")
            with gr.Tabs():
                with gr.TabItem("廠商"):
                    sup_edit_id = gr.State(value=None)
                    with gr.Row():
                        sup_name = gr.Textbox(label="廠商名稱", scale=3)
                        sup_add_btn = gr.Button("新增廠商", scale=1)
                    with gr.Row():
                        sup_update_btn = gr.Button("修改所選")
                        sup_delete_btn = gr.Button("刪除所選", variant="stop")
                    sup_msg = gr.Markdown("")
                    sup_list = gr.Dataframe(value=get_basic_list("suppliers"), headers=["ID", "名稱"], row_count=(1, "dynamic"), interactive=False)

                with gr.TabItem("貨品"):
                    pro_edit_id = gr.State(value=None)
                    with gr.Row():
                        pro_name = gr.Textbox(label="貨品名稱", scale=3)
                        pro_add_btn = gr.Button("新增貨品", scale=1)
                    with gr.Row():
                        pro_update_btn = gr.Button("修改所選")
                        pro_delete_btn = gr.Button("刪除所選", variant="stop")
                    pro_msg = gr.Markdown("")
                    pro_list = gr.Dataframe(value=get_basic_list("products"), headers=["ID", "名稱"], row_count=(1, "dynamic"), interactive=False)

                with gr.TabItem("顧客"):
                    cus_edit_id = gr.State(value=None)
                    with gr.Row():
                        cus_name = gr.Textbox(label="顧客名稱", scale=3)
                        cus_add_btn = gr.Button("新增顧客", scale=1)
                    with gr.Row():
                        cus_update_btn = gr.Button("修改所選")
                        cus_delete_btn = gr.Button("刪除所選", variant="stop")
                    cus_msg = gr.Markdown("")
                    cus_list = gr.Dataframe(value=get_basic_list("customers"), headers=["ID", "名稱"], row_count=(1, "dynamic"), interactive=False)

                with gr.TabItem("幣別"):
                    cur_edit_id = gr.State(value=None)
                    with gr.Row():
                        cur_name = gr.Textbox(label="幣別名稱", scale=3)
                        cur_add_btn = gr.Button("新增幣別", scale=1)
                    with gr.Row():
                        cur_update_btn = gr.Button("修改所選")
                        cur_delete_btn = gr.Button("刪除所選", variant="stop")
                    cur_msg = gr.Markdown("")
                    cur_list = gr.Dataframe(value=get_basic_list("currency"), headers=["ID", "名稱"], row_count=(1, "dynamic"), interactive=False)

                with gr.TabItem("匯率"):
                    gr.Markdown("登記每種幣別在每個年月對台幣的匯率（台幣本身不用登記，系統自動視為 1:1）。")
                    fx_edit_id = gr.State(value=None)
                    with gr.Row():
                        fx_cur_dp = gr.Dropdown(choices=get_options("currency"), label="幣別", interactive=True)
                        fx_year_tb = gr.Textbox(label="年份", placeholder="例如 2026")
                        fx_month_tb = gr.Textbox(label="月份", placeholder="例如 09")
                        fx_rate_tb = gr.Textbox(label="匯率（1 單位該幣別 = ？ 台幣）")
                    with gr.Row():
                        fx_save_btn = gr.Button("登記 / 更新匯率")
                        fx_delete_btn = gr.Button("刪除此筆", variant="stop")
                    fx_msg = gr.Markdown("")
                    fx_display_init, fx_raw_init = get_exchange_rate_list()
                    fx_list = gr.Dataframe(
                        value=fx_display_init,
                        headers=["幣別", "年份", "月份", "匯率"],
                        row_count=(1, "dynamic"),
                        interactive=False,
                    )
                    fx_raw_state = gr.State(value=fx_raw_init)

        with gr.TabItem("儀表板", id="dashboard") as dashboard_tab:
            gr.Markdown(
                "月結損益總覽與趨勢圖。走勢圖金額均為台幣，包含出貨、進貨、毛利、淨利及盤點死傷估值；"
                "死傷僅統計已登記實際盤點且庫存短少的品項。"
            )
            with gr.Row():
                dash_year_months = get_inventory_year_months()
                if dash_year_months:
                    dash_latest_year, dash_latest_month = dash_year_months[0]
                else:
                    now = datetime.now()
                    dash_latest_year, dash_latest_month = now.year, now.month
                dash_available_years = sorted({str(y) for y, m in dash_year_months}, reverse=True)
                if not dash_available_years:
                    dash_available_years = [str(dash_latest_year)]

                dash_year_dp = gr.Dropdown(choices=dash_available_years, value=str(dash_latest_year), label="年份", interactive=True)
                dash_month_dp = gr.Dropdown(
                    choices=[f"{m:02d}" for m in range(1, 13)],
                    value=f"{dash_latest_month:02d}",
                    label="月份",
                    interactive=True,
                )
                dash_query_btn = gr.Button("查詢")

            dash_summary = gr.Markdown(format_dashboard_summary(str(dash_latest_year), f"{dash_latest_month:02d}"))

            gr.Markdown("<h4 style='margin-top: 20px;'>本月其他支出（薪水、租金等）</h4>")
            expense_edit_id = gr.State(value=None)
            with gr.Row():
                expense_name_tb = gr.Textbox(label="項目名稱", scale=2)
                expense_amount_tb = gr.Textbox(label="金額", scale=1)
            with gr.Row():
                expense_add_btn = gr.Button("新增支出")
                expense_update_btn = gr.Button("修改所選")
                expense_delete_btn = gr.Button("刪除所選", variant="stop")
            expense_msg = gr.Markdown("")
            expense_display_init, expense_raw_init = get_expenses_table(str(dash_latest_year), f"{dash_latest_month:02d}")
            expense_table = gr.Dataframe(
                value=expense_display_init,
                headers=["項目", "金額"],
                row_count=(1, "dynamic"),
                interactive=False,
            )
            expense_raw_state = gr.State(value=expense_raw_init)

            gr.Markdown("<h4 style='margin-top: 30px;'>走勢圖</h4>")
            trend_mode_dp = gr.Radio(choices=["近12個月", "近5年", "近10年"], value="近12個月", label="檢視方式")
            trend_series_cbg = gr.CheckboxGroup(
                choices=["出貨", "進貨", "毛利", "淨利", "死傷估值", "死傷（公斤）"],
                value=["出貨", "進貨", "毛利", "淨利", "死傷估值", "死傷（公斤）"],
                label="顯示哪些線條",
            )
            trend_main_init = get_trend_chart_data("近12個月")
            trend_loss_init = get_inventory_loss_trend_data("近12個月")
            trend_main_state = gr.State(value=trend_main_init)
            trend_loss_state = gr.State(value=trend_loss_init)
            trend_chart = gr.LinePlot(
                value=trend_main_init,
                x="期間",
                y="金額",
                color="類別",
                height=350,
            )
            with gr.Column(visible=True) as loss_chart_box:
                gr.Markdown("#### 死傷數量趨勢（僅公斤）")
                loss_trend_chart = gr.LinePlot(
                    value=trend_loss_init,
                    x="期間",
                    y="公斤",
                    color="類別",
                    height=250,
                )

        with gr.TabItem("應收/應付帳款", id="receivable_payable") as receivable_payable_tab:
            with gr.Tabs():
                with gr.TabItem("應收"):
                    gr.Markdown("每個客戶累計出貨總額、已收金額，以及還沒收到的應收款餘額。")
                    rec_balance_init = get_customer_balances()
                    rec_balance_table = gr.Dataframe(
                        value=rec_balance_init,
                        headers=["客戶", "累計出貨總額", "已收金額", "應收款餘額"],
                        row_count=(1, "dynamic"),
                        interactive=False,
                    )

                    gr.Markdown("<h4 style='margin-top: 30px;'>登記收款</h4>")
                    rec_edit_id = gr.State(value=None)
                    with gr.Row():
                        rec_cus_dp = gr.Dropdown(choices=get_options("customers"), label="客戶", interactive=True)
                        rec_date_tb = gr.Textbox(label="日期", value=datetime.now().strftime("%Y-%m-%d"))
                        rec_amount_tb = gr.Textbox(label="收款金額")
                        rec_note_tb = gr.Textbox(label="備註（選填）")
                    with gr.Row():
                        rec_add_btn = gr.Button("新增收款")
                        rec_update_btn = gr.Button("修改所選")
                        rec_delete_btn = gr.Button("刪除所選", variant="stop")
                    rec_msg = gr.Markdown("")

                    gr.Markdown("<h4 style='margin-top: 20px;'>收款紀錄（點一列可選取來修改或刪除）</h4>")
                    rec_payment_display_init, rec_payment_raw_init = get_receivable_payments_list()
                    rec_payment_table = gr.Dataframe(
                        value=rec_payment_display_init,
                        headers=["客戶", "日期", "金額", "備註"],
                        row_count=(1, "dynamic"),
                        interactive=False,
                    )
                    rec_payment_raw_state = gr.State(value=rec_payment_raw_init)

                with gr.TabItem("應付"):
                    gr.Markdown(
                        "外幣付款請依序選擇：**廠商 → 本次付款幣別 → 未付款進貨 → 實際匯出台幣總額**。\n\n"
                        "一次付款只能處理一種幣別，系統會反推單一實際匯率，並按原始外幣金額分攤到每筆進貨。"
                    )
                    pay_balance_init = get_supplier_balances()
                    pay_balance_table = gr.Dataframe(
                        value=pay_balance_init,
                        headers=["廠商", "累計進貨總額(台幣)", "已付金額(台幣)", "應付款餘額(台幣)"],
                        row_count=(1, "dynamic"),
                        interactive=False,
                    )

                    gr.Markdown("<h4 style='margin-top: 30px;'>登記付款</h4>")
                    pay_edit_id = gr.State(value=None)
                    with gr.Row():
                        pay_sup_dp = gr.Dropdown(choices=get_options("suppliers"), label="1. 廠商", interactive=True)
                        pay_currency_dp = gr.Dropdown(choices=[], label="2. 本次付款幣別", interactive=True)
                        pay_date_tb = gr.Textbox(label="日期", value=datetime.now().strftime("%Y-%m-%d"))
                        pay_amount_tb = gr.Textbox(label="4. 實際匯出台幣總額")
                        pay_note_tb = gr.Textbox(label="備註（選填）")
                    pay_unalloc_cbg = gr.CheckboxGroup(choices=[], label="3. 勾選本次合併付款的未付款進貨")
                    pay_preview = gr.Markdown("請依序選擇廠商、付款幣別與進貨紀錄。")
                    with gr.Row():
                        pay_add_btn = gr.Button("計算並登記付款")
                        pay_delete_btn = gr.Button("刪除所選", variant="stop")
                    pay_msg = gr.Markdown("")

                    gr.Markdown("<h4 style='margin-top: 20px;'>付款紀錄（點一列可選取來刪除，下方會顯示分配明細）</h4>")
                    pay_payment_display_init, pay_payment_raw_init = get_payable_payments_list()
                    pay_payment_table = gr.Dataframe(
                        value=pay_payment_display_init,
                        headers=["廠商", "日期", "付款幣別", "外幣合計", "匯出台幣", "實際匯率", "備註"],
                        row_count=(1, "dynamic"),
                        interactive=False,
                    )
                    pay_payment_raw_state = gr.State(value=pay_payment_raw_init)

                    gr.Markdown("<h4 style='margin-top: 20px;'>選取付款的分配明細</h4>")
                    pay_allocation_table = gr.Dataframe(
                        value=[],
                        headers=["進貨日期", "貨品", "原始金額(外幣)", "幣別", "換算匯率", "台幣分攤金額"],
                        row_count=(1, "dynamic"),
                        interactive=False,
                    )

        with gr.TabItem("匯出/匯入", id="export_import") as export_import_tab:
            export_years = get_export_year_choices()
            export_months = [ALL_MONTHS_LABEL] + [f"{m:02d}" for m in range(1, 13)]
            this_month = f"{datetime.now().month:02d}"
            with gr.Tabs():
                with gr.TabItem("匯出"):
                    gr.Markdown("選擇條件後按下按鈕，會產生 Excel 檔（.xlsx），再點檔案即可下載。")

                    gr.Markdown("<h3>請款單</h3>客戶某月的出貨明細，並列出前期未收、本期已收與本期應收總額。")
                    with gr.Row():
                        inv_cus_dp = gr.Dropdown(choices=get_options("customers"), label="客戶", interactive=True)
                        inv_year_dp = gr.Dropdown(choices=export_years, value=export_years[0], label="年份", interactive=True)
                        inv_month_dp = gr.Dropdown(choices=export_months[1:], value=this_month, label="月份", interactive=True)
                        inv_export_btn = gr.Button("產生請款單", variant="primary")

                    gr.Markdown("<h3 style='margin-top: 30px;'>出貨表</h3>")
                    with gr.Row():
                        exp_sale_cus_dp = gr.Dropdown(choices=[("全部客戶", 0)] + get_options("customers"), value=0, label="客戶", interactive=True)
                        exp_sale_year_dp = gr.Dropdown(choices=export_years, value=export_years[0], label="年份", interactive=True)
                        exp_sale_month_dp = gr.Dropdown(choices=export_months, value=this_month, label="月份", interactive=True)
                        exp_sale_btn = gr.Button("匯出出貨表", variant="primary")

                    gr.Markdown("<h3 style='margin-top: 30px;'>進貨表</h3>")
                    with gr.Row():
                        exp_pur_sup_dp = gr.Dropdown(choices=[("全部廠商", 0)] + get_options("suppliers"), value=0, label="廠商", interactive=True)
                        exp_pur_year_dp = gr.Dropdown(choices=export_years, value=export_years[0], label="年份", interactive=True)
                        exp_pur_month_dp = gr.Dropdown(choices=export_months, value=this_month, label="月份", interactive=True)
                        exp_pur_btn = gr.Button("匯出進貨表", variant="primary")

                    export_msg = gr.Markdown("")
                    export_file = gr.File(label="下載檔案", interactive=False)

                with gr.TabItem("匯入"):
                    gr.Markdown(
                        "批次匯入資料，不用一筆筆手動輸入。步驟：\n"
                        "1. 選擇要匯入的資料類型，下載範本\n"
                        "2. 用 Excel 填好資料後上傳（支援 .xlsx、.csv）\n"
                        "3. 按「檢查資料」確認每一列都正確，再按「確認匯入」\n\n"
                        "客戶／廠商／貨品／幣別要先存在於「基礎資料設定」中，名稱必須完全一樣。"
                    )
                    with gr.Row():
                        import_type_dp = gr.Dropdown(choices=list(IMPORT_TYPES), value="出貨", label="資料類型", interactive=True)
                        template_btn = gr.Button("下載範本")
                    template_file = gr.File(label="範本檔", interactive=False)
                    import_file = gr.File(label="上傳要匯入的檔案", file_types=[".xlsx", ".csv"], type="filepath")
                    with gr.Row():
                        import_check_btn = gr.Button("檢查資料")
                        import_confirm_btn = gr.Button("確認匯入", variant="primary")
                    import_msg = gr.Markdown("")
                    import_valid_state = gr.State(value=[])
                    import_preview = gr.Dataframe(
                        value=pd.DataFrame(columns=["列號", "狀態"] + IMPORT_TYPES["出貨"]),
                        label="有錯誤或需要注意的資料列（都沒問題時是空的）",
                        interactive=False,
                        wrap=True,
                    )

        with gr.TabItem("備份/還原", id="backup_restore") as backup_restore_tab:
            with gr.Tabs():
                with gr.TabItem("備份"):
                    gr.Markdown(
                        "把整個資料庫（所有資料表）存成一個 .sql 檔，放在備份資料夾，也可以直接下載一份。\n\n"
                        "建議在大量匯入、刪除資料、或每週固定備份一次，並把備份檔另外複製到隨身碟或雲端。"
                    )
                    backup_btn = gr.Button("立即備份", variant="primary")
                    backup_msg = gr.Markdown("")
                    backup_file = gr.File(label="下載這次的備份", interactive=False)
                    gr.Markdown(f"<h4 style='margin-top: 20px;'>備份資料夾裡的檔案</h4>存放位置：`{BACKUP_DIR}`")
                    backup_table = gr.Dataframe(
                        value=[list(i) for i in list_backups()],
                        headers=["檔名", "大小(KB)", "時間"],
                        interactive=False,
                        wrap=True,
                    )

                with gr.TabItem("還原"):
                    gr.Markdown(
                        "用備份檔把資料庫**整個換回備份當時的樣子**。步驟：\n"
                        "1. 從下拉選單選一個備份，或上傳備份檔（上傳的優先）\n"
                        "2. 按「檢查備份檔」，確認每張表的筆數是你要的\n"
                        f"3. 在確認欄輸入「{RESTORE_CONFIRM_WORD}」，按「確認還原」\n\n"
                        "還原前會自動再備份一次目前的資料。還原時請避免同時有人在操作。"
                    )
                    restore_choice_dp = gr.Dropdown(
                        choices=[i[0] for i in list_backups()], label="從備份資料夾選擇", interactive=True
                    )
                    restore_upload = gr.File(label="或上傳備份檔（.sql）", file_types=[".sql"], type="filepath")
                    restore_check_btn = gr.Button("檢查備份檔")
                    restore_info = gr.Markdown("")
                    restore_compare = gr.Dataframe(
                        value=pd.DataFrame(columns=["資料表", "備份檔筆數", "目前筆數"]),
                        label="備份檔 vs 目前資料庫",
                        interactive=False,
                        wrap=True,
                    )
                    restore_path_state = gr.State(value=None)
                    restore_confirm_tb = gr.Textbox(label=f"確認欄（輸入「{RESTORE_CONFIRM_WORD}」）")
                    restore_btn = gr.Button("確認還原", variant="stop")
                    restore_msg = gr.Markdown("")

    # ---------- 進貨 ----------
    insert.click(
        fn=insert_purchas,
        inputs=[sup_dp, pro_dp, qua, unit_pri, total, unit_dp, Cur_dp, year_dp, month_dp],
        outputs=[purchases_table, purchase_msg, purchases_raw_state],
    )
    refresh_btn.click(fn=refresh_purchases_table, inputs=[year_dp, month_dp], outputs=[purchases_table, purchases_raw_state])
    qua.change(fn=auto_calculate_total, inputs=[qua, unit_pri], outputs=total)
    unit_pri.change(fn=auto_calculate_total, inputs=[qua, unit_pri], outputs=total)

    purchases_table.select(
        fn=select_purchase_row,
        inputs=purchases_raw_state,
        outputs=[purchase_edit_id, sup_dp, pro_dp, qua, unit_pri, total, unit_dp, Cur_dp, purchase_msg],
    )
    purchase_update_btn.click(
        fn=update_purchase,
        inputs=[purchase_edit_id, sup_dp, pro_dp, qua, unit_pri, total, unit_dp, Cur_dp, year_dp, month_dp],
        outputs=[purchases_table, purchase_msg, purchases_raw_state, purchase_edit_id],
    )
    purchase_delete_btn.click(
        fn=delete_purchase,
        inputs=[purchase_edit_id, year_dp, month_dp],
        outputs=[purchases_table, purchase_msg, purchases_raw_state, purchase_edit_id],
    )

    # ---------- 出貨 ----------
    insert2.click(
        fn=insert_sale,
        inputs=[cus_dp, pro_dp2, qua2, unit_pri2, total2, unit_dp2, year_dp2, month_dp2],
        outputs=[sales_table, sale_msg, sales_raw_state],
    )
    refresh_btn2.click(fn=refresh_sales_table, inputs=[year_dp2, month_dp2], outputs=[sales_table, sales_raw_state])
    qua2.change(fn=auto_calculate_total, inputs=[qua2, unit_pri2], outputs=total2)
    unit_pri2.change(fn=auto_calculate_total, inputs=[qua2, unit_pri2], outputs=total2)

    sales_table.select(
        fn=select_sale_row,
        inputs=sales_raw_state,
        outputs=[sale_edit_id, cus_dp, pro_dp2, qua2, unit_pri2, total2, unit_dp2, sale_msg],
    )
    sale_update_btn.click(
        fn=update_sale,
        inputs=[sale_edit_id, cus_dp, pro_dp2, qua2, unit_pri2, total2, unit_dp2, year_dp2, month_dp2],
        outputs=[sales_table, sale_msg, sales_raw_state, sale_edit_id],
    )
    sale_delete_btn.click(
        fn=delete_sale,
        inputs=[sale_edit_id, year_dp2, month_dp2],
        outputs=[sales_table, sale_msg, sales_raw_state, sale_edit_id],
    )

    # ---------- 庫存 ----------
    inv_refresh_btn.click(
        fn=refresh_inventory_table,
        inputs=[inv_year_dp, inv_month_dp],
        outputs=[inventory_table, inventory_raw_state],
    )
    inventory_table.select(
        fn=select_inventory_row,
        inputs=inventory_raw_state,
        outputs=[inv_pro_dp, inv_unit_dp, inv_actual_qty, inv_msg, inventory_selected_state],
    )
    inv_unit_dp.change(
        fn=switch_inventory_unit,
        inputs=[inv_unit_dp, inventory_selected_state],
        outputs=inv_actual_qty,
    )
    inv_save_btn.click(
        fn=save_inventory_count,
        inputs=[inv_pro_dp, inv_unit_dp, inv_actual_qty, inv_year_dp, inv_month_dp],
        outputs=[inventory_table, inv_msg, inventory_raw_state],
    )
    inv_clear_btn.click(
        fn=clear_inventory_count,
        inputs=[inv_pro_dp, inv_unit_dp, inv_year_dp, inv_month_dp],
        outputs=[inventory_table, inv_msg, inventory_raw_state],
    )
    inventory_tab.select(fn=refresh_inventory_product_dropdown, outputs=[inv_pro_dp])

    # ---------- 基礎資料 ----------
    sup_add_btn.click(fn=insert_supplier, inputs=sup_name, outputs=[sup_list, sup_name, sup_msg])
    sup_list.select(fn=select_basic_row, inputs=sup_list, outputs=[sup_edit_id, sup_name, sup_msg])
    sup_update_btn.click(fn=update_supplier, inputs=[sup_edit_id, sup_name], outputs=[sup_list, sup_edit_id, sup_name, sup_msg])
    sup_delete_btn.click(fn=delete_supplier, inputs=sup_edit_id, outputs=[sup_list, sup_edit_id, sup_name, sup_msg])

    pro_add_btn.click(fn=insert_product, inputs=pro_name, outputs=[pro_list, pro_name, pro_msg])
    pro_list.select(fn=select_basic_row, inputs=pro_list, outputs=[pro_edit_id, pro_name, pro_msg])
    pro_update_btn.click(fn=update_product, inputs=[pro_edit_id, pro_name], outputs=[pro_list, pro_edit_id, pro_name, pro_msg])
    pro_delete_btn.click(fn=delete_product, inputs=pro_edit_id, outputs=[pro_list, pro_edit_id, pro_name, pro_msg])

    cus_add_btn.click(fn=insert_customer, inputs=cus_name, outputs=[cus_list, cus_name, cus_msg])
    cus_list.select(fn=select_basic_row, inputs=cus_list, outputs=[cus_edit_id, cus_name, cus_msg])
    cus_update_btn.click(fn=update_customer, inputs=[cus_edit_id, cus_name], outputs=[cus_list, cus_edit_id, cus_name, cus_msg])
    cus_delete_btn.click(fn=delete_customer, inputs=cus_edit_id, outputs=[cus_list, cus_edit_id, cus_name, cus_msg])

    cur_add_btn.click(fn=insert_currency, inputs=cur_name, outputs=[cur_list, cur_name, cur_msg])
    cur_list.select(fn=select_basic_row, inputs=cur_list, outputs=[cur_edit_id, cur_name, cur_msg])
    cur_update_btn.click(fn=update_currency, inputs=[cur_edit_id, cur_name], outputs=[cur_list, cur_edit_id, cur_name, cur_msg])
    cur_delete_btn.click(fn=delete_currency, inputs=cur_edit_id, outputs=[cur_list, cur_edit_id, cur_name, cur_msg])

    # ---------- 基礎資料：匯率 ----------
    fx_list.select(
        fn=select_exchange_rate_row,
        inputs=fx_raw_state,
        outputs=[fx_edit_id, fx_cur_dp, fx_year_tb, fx_month_tb, fx_rate_tb, fx_msg],
    )
    fx_save_btn.click(
        fn=save_exchange_rate,
        inputs=[fx_edit_id, fx_cur_dp, fx_year_tb, fx_month_tb, fx_rate_tb],
        outputs=[fx_list, fx_msg, fx_raw_state, fx_edit_id],
    )
    fx_delete_btn.click(
        fn=delete_exchange_rate,
        inputs=fx_edit_id,
        outputs=[fx_list, fx_msg, fx_raw_state, fx_edit_id],
    )

    # ---------- 儀表板 ----------
    dash_query_btn.click(
        fn=refresh_dashboard,
        inputs=[dash_year_dp, dash_month_dp],
        outputs=[dash_summary, expense_table, expense_raw_state],
    )
    expense_table.select(
        fn=select_expense_row,
        inputs=expense_raw_state,
        outputs=[expense_edit_id, expense_name_tb, expense_amount_tb, expense_msg],
    )
    expense_add_btn.click(
        fn=insert_expense,
        inputs=[dash_year_dp, dash_month_dp, expense_name_tb, expense_amount_tb],
        outputs=[dash_summary, expense_table, expense_raw_state, expense_msg, expense_name_tb, expense_amount_tb],
    )
    expense_update_btn.click(
        fn=update_expense,
        inputs=[expense_edit_id, dash_year_dp, dash_month_dp, expense_name_tb, expense_amount_tb],
        outputs=[dash_summary, expense_table, expense_raw_state, expense_msg, expense_edit_id, expense_name_tb, expense_amount_tb],
    )
    expense_delete_btn.click(
        fn=delete_expense,
        inputs=[expense_edit_id, dash_year_dp, dash_month_dp],
        outputs=[dash_summary, expense_table, expense_raw_state, expense_msg, expense_edit_id, expense_name_tb, expense_amount_tb],
    )
    trend_mode_dp.change(
        fn=refresh_trend_chart,
        inputs=[trend_mode_dp, trend_series_cbg],
        outputs=[trend_chart, loss_trend_chart, loss_chart_box, trend_main_state, trend_loss_state],
    )
    trend_series_cbg.change(
        fn=apply_trend_filter,
        inputs=[trend_series_cbg, trend_main_state, trend_loss_state],
        outputs=[trend_chart, loss_chart_box],
    )
    dashboard_tab.select(fn=refresh_dashboard_year_choices, outputs=dash_year_dp)
    # 走勢圖資料是啟動時算好存在 State 裡的，點進分頁時要重算，否則新增/匯入的資料不會出現
    dashboard_tab.select(
        fn=refresh_trend_chart,
        inputs=[trend_mode_dp, trend_series_cbg],
        outputs=[trend_chart, loss_trend_chart, loss_chart_box, trend_main_state, trend_loss_state],
    )

    # ---------- 進貨/出貨/庫存分頁：每次點進去都重新查一次下拉選單 ----------
    purchases_tab.select(fn=refresh_purchase_dropdowns, outputs=[sup_dp, pro_dp, Cur_dp])
    sales_tab.select(fn=refresh_sales_dropdowns, outputs=[cus_dp, pro_dp2])

    # ---------- 應收帳款 ----------
    rec_payment_table.select(
        fn=select_receivable_row,
        inputs=rec_payment_raw_state,
        outputs=[rec_edit_id, rec_cus_dp, rec_date_tb, rec_amount_tb, rec_note_tb, rec_msg],
    )
    rec_add_btn.click(
        fn=insert_receivable_payment,
        inputs=[rec_cus_dp, rec_date_tb, rec_amount_tb, rec_note_tb],
        outputs=[rec_balance_table, rec_payment_table, rec_payment_raw_state, rec_msg],
    )
    rec_update_btn.click(
        fn=update_receivable_payment,
        inputs=[rec_edit_id, rec_cus_dp, rec_date_tb, rec_amount_tb, rec_note_tb],
        outputs=[rec_balance_table, rec_payment_table, rec_payment_raw_state, rec_msg, rec_edit_id],
    )
    rec_delete_btn.click(
        fn=delete_receivable_payment,
        inputs=rec_edit_id,
        outputs=[rec_balance_table, rec_payment_table, rec_payment_raw_state, rec_msg, rec_edit_id],
    )

    # ---------- 應付帳款 ----------
    pay_sup_dp.change(
        fn=refresh_payment_currency_choices,
        inputs=pay_sup_dp,
        outputs=[pay_currency_dp, pay_unalloc_cbg, pay_preview],
    )
    pay_currency_dp.change(
        fn=refresh_unallocated_choices,
        inputs=[pay_sup_dp, pay_currency_dp],
        outputs=[pay_unalloc_cbg, pay_preview],
    )
    pay_unalloc_cbg.change(
        fn=preview_payable_payment,
        inputs=[pay_sup_dp, pay_currency_dp, pay_unalloc_cbg, pay_amount_tb],
        outputs=pay_preview,
    )
    pay_amount_tb.change(
        fn=preview_payable_payment,
        inputs=[pay_sup_dp, pay_currency_dp, pay_unalloc_cbg, pay_amount_tb],
        outputs=pay_preview,
    )
    pay_payment_table.select(
        fn=select_payable_row,
        inputs=pay_payment_raw_state,
        outputs=[pay_edit_id, pay_allocation_table, pay_msg],
    )
    pay_add_btn.click(
        fn=insert_payable_payment,
        inputs=[pay_sup_dp, pay_currency_dp, pay_date_tb, pay_amount_tb, pay_unalloc_cbg, pay_note_tb],
        outputs=[pay_balance_table, pay_payment_table, pay_payment_raw_state, pay_msg, pay_currency_dp, pay_unalloc_cbg, pay_allocation_table, pay_preview],
    )
    pay_delete_btn.click(
        fn=delete_payable_payment,
        inputs=[pay_edit_id, pay_sup_dp, pay_currency_dp],
        outputs=[pay_balance_table, pay_payment_table, pay_payment_raw_state, pay_msg, pay_edit_id, pay_allocation_table, pay_unalloc_cbg, pay_preview],
    )

    # ---------- 應收/應付帳款：每次點進這個分頁，重新查一次下拉選單跟表格 ----------
    def refresh_receivable_payable_page():
        rec_balance = get_customer_balances()
        rec_payment_display, rec_payment_raw = get_receivable_payments_list()
        pay_balance = get_supplier_balances()
        pay_payment_display, pay_payment_raw = get_payable_payments_list()
        return (
            gr.update(choices=get_options("customers")),
            gr.update(choices=get_options("suppliers")),
            gr.update(choices=[], value=None),
            gr.update(value=rec_balance),
            gr.update(value=rec_payment_display),
            rec_payment_raw,
            gr.update(value=pay_balance),
            gr.update(value=pay_payment_display),
            pay_payment_raw,
            gr.update(choices=[], value=[]),
            "請依序選擇廠商、付款幣別與進貨紀錄。",
        )

    receivable_payable_tab.select(
        fn=refresh_receivable_payable_page,
        outputs=[
            rec_cus_dp, pay_sup_dp, pay_currency_dp,
            rec_balance_table, rec_payment_table, rec_payment_raw_state,
            pay_balance_table, pay_payment_table, pay_payment_raw_state,
            pay_unalloc_cbg, pay_preview,
        ],
    )

    # ---------- 匯出/匯入 ----------
    inv_export_btn.click(
        fn=export_invoice_excel,
        inputs=[inv_cus_dp, inv_year_dp, inv_month_dp],
        outputs=[export_file, export_msg],
    )
    exp_sale_btn.click(
        fn=export_sales_excel,
        inputs=[exp_sale_year_dp, exp_sale_month_dp, exp_sale_cus_dp],
        outputs=[export_file, export_msg],
    )
    exp_pur_btn.click(
        fn=export_purchases_excel,
        inputs=[exp_pur_year_dp, exp_pur_month_dp, exp_pur_sup_dp],
        outputs=[export_file, export_msg],
    )
    export_import_tab.select(
        fn=refresh_export_tab,
        outputs=[inv_year_dp, exp_sale_year_dp, exp_pur_year_dp, inv_cus_dp, exp_sale_cus_dp, exp_pur_sup_dp],
    )

    # ---------- 備份/還原 ----------
    backup_btn.click(fn=do_backup, outputs=[backup_file, backup_msg, backup_table, restore_choice_dp])
    backup_restore_tab.select(fn=backup_list_updates, outputs=[backup_table, restore_choice_dp])
    restore_check_btn.click(
        fn=inspect_backup,
        inputs=[restore_upload, restore_choice_dp],
        outputs=[restore_compare, restore_path_state, restore_info],
    )
    # 換了檔案就要重新檢查，避免檢查的是 A 檔、實際還原的卻是 B 檔
    for _src in (restore_upload, restore_choice_dp):
        _src.change(
            fn=lambda: (pd.DataFrame(columns=["資料表", "備份檔筆數", "目前筆數"]), None, ""),
            outputs=[restore_compare, restore_path_state, restore_info],
        )
    restore_btn.click(
        fn=do_restore,
        inputs=[restore_path_state, restore_confirm_tb],
        outputs=[restore_msg, backup_table, restore_choice_dp, restore_confirm_tb],
    )

    template_btn.click(fn=download_import_template, inputs=import_type_dp, outputs=template_file)
    # 換資料類型或換檔案時，清掉上一次的檢查結果，避免誤匯入舊的資料
    import_type_dp.change(fn=reset_import_preview, inputs=import_type_dp, outputs=[import_preview, import_valid_state, import_msg])
    import_file.change(fn=reset_import_preview, inputs=import_type_dp, outputs=[import_preview, import_valid_state, import_msg])
    import_check_btn.click(
        fn=validate_import,
        inputs=[import_file, import_type_dp],
        outputs=[import_preview, import_valid_state, import_msg],
    )
    import_confirm_btn.click(
        fn=confirm_import,
        inputs=[import_type_dp, import_valid_state],
        outputs=[import_msg, import_valid_state],
    )


try:
    demo.launch()
finally:
    mycursor.close()
    mydb.close()