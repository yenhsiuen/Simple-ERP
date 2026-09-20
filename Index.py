import pymysql
import gradio as gr
import pandas as pd
from datetime import datetime

mydb = pymysql.connect(host="localhost", user="root", password="4567", database="川海erp")
mycursor = mydb.cursor()


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
    """進貨總額換算成台幣；依每一筆交易當月的匯率換算，回傳 (總額, 使用估計匯率的幣別名稱集合)"""
    if month is not None:
        sql = """
            SELECT MONTH(transaction_time) AS m, currency_id, SUM(total_price)
            FROM purchases WHERE YEAR(transaction_time)=%s AND MONTH(transaction_time)=%s
            GROUP BY MONTH(transaction_time), currency_id
        """
        params = (int(year), int(month))
    else:
        sql = """
            SELECT MONTH(transaction_time) AS m, currency_id, SUM(total_price)
            FROM purchases WHERE YEAR(transaction_time)=%s
            GROUP BY MONTH(transaction_time), currency_id
        """
        params = (int(year),)
    mycursor.execute(sql, params)
    total = 0.0
    estimated_currencies = set()
    for m, currency_id, amt in mycursor.fetchall():
        rate, is_estimated = get_exchange_rate(currency_id, year, m)
        if is_estimated:
            estimated_currencies.add(get_currency_name(currency_id))
        total += float(amt) * rate
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
        else:
            gross_val = round(sales - purchases, 2)
            net_val = round(gross_val - expenses, 2)
        rows.append({"期間": label, "類別": "出貨", "金額": sales})
        rows.append({"期間": label, "類別": "進貨", "金額": purchases})
        rows.append({"期間": label, "類別": "淨利", "金額": net_val})
    return pd.DataFrame(rows)


def refresh_trend_chart(mode):
    return gr.update(value=get_trend_chart_data(mode))


def refresh_dashboard_year_choices():
    """點進『儀表板』分頁時，重新查一次年份選單，確保有最新一年可以選"""
    year_months = get_inventory_year_months()
    years = sorted({str(y) for y, m in year_months}, reverse=True)
    if not years:
        years = [str(datetime.now().year)]
    return gr.update(choices=years)


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


with gr.Blocks() as demo:
    with gr.Tabs() as tabs:
        with gr.TabItem("首頁", id="home"):
            gr.Markdown(
                "## 歡迎使用川海 ERP\n\n"
                "請從上方分頁選擇「出貨」、「進貨」、「庫存」或「基礎資料設定」開始操作。\n\n"
                "廠商／貨品／顧客／幣別的新增、修改、刪除，都可在本頁面的「基礎資料設定」分頁完成。"
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
            gr.Markdown("月結損益總覽，以及出貨/進貨/淨利的走勢圖。")
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
            trend_chart = gr.LinePlot(
                value=get_trend_chart_data("近12個月"),
                x="期間",
                y="金額",
                color="類別",
                height=350,
            )

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
    trend_mode_dp.change(fn=refresh_trend_chart, inputs=trend_mode_dp, outputs=trend_chart)
    dashboard_tab.select(fn=refresh_dashboard_year_choices, outputs=dash_year_dp)

    # ---------- 進貨/出貨/庫存分頁：每次點進去都重新查一次下拉選單 ----------
    purchases_tab.select(fn=refresh_purchase_dropdowns, outputs=[sup_dp, pro_dp, Cur_dp])
    sales_tab.select(fn=refresh_sales_dropdowns, outputs=[cus_dp, pro_dp2])


try:
    demo.launch()
finally:
    mycursor.close()
    mydb.close()