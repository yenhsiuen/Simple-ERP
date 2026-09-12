import pymysql
import gradio as gr
from datetime import datetime
mydb = pymysql.connect(host="localhost", user="root", password="4567", database="川海erp")
mycursor = mydb.cursor()

def get_options(item):

  # 同時抓取 id 與 name
  sql = "SELECT id, name FROM %s" % item
  mycursor.execute(sql)
  results = mycursor.fetchall()
  
  # 組合成 [(名稱, ID)] 的格式，例如 [('越南阿龍水產', 'SUP001'), ('印尼蟹商', 'SUP002')]
  options = [(row[1], row[0]) for row in results]
  return options


def insert_purchas(n1,n2,n3,n4,n5,n6,n7):

  sql = """
      INSERT INTO purchases
      (supplier_id, product_id, quantity, unit_price, total_price, unit, currency_id)
      VALUES (%s, %s, %s, %s, %s, %s, %s)
  """

  mycursor.execute(sql, (n1, n2, n3, n4, n5, n6, n7))

  mydb.commit()
  print(mycursor.rowcount,"資料儲存成功")


def insert_sale(n1,n2,n3,n4,n5,n6):

  sql = """
      INSERT INTO sales
      (customer_id, product_id, quantity, unit_price, total_price, unit)
      VALUES (%s, %s, %s, %s, %s, %s)
  """

  mycursor.execute(sql, (n1, n2, n3, n4, n5, n6))

  mydb.commit()
  print(mycursor.rowcount,"資料儲存成功")


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
    # 回傳格式例如 [(2026, 8), (2026, 6), (2025, 12), ...]
    return [(row[0], row[1]) for row in rows]


def get_purchases_table(year, month):
    """依年月查詢進貨紀錄，回傳純資料（list of lists）給表格顯示"""
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
        ORDER BY pu.transaction_time DESC
    """
    mycursor.execute(sql, (year, int(month)))
    rows = mycursor.fetchall()
    return [list(r) for r in rows]


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
    """依年月查詢出貨紀錄，回傳純資料（list of lists）給表格顯示"""
    sql = """
        SELECT sa.transaction_time, c.name AS 客戶, p.name AS 貨品,
               sa.quantity AS 數量, sa.unit AS 單位,
               sa.unit_price AS 單價, sa.total_price AS 總額
        FROM sales sa
        LEFT JOIN customers c ON sa.customer_id = c.id
        LEFT JOIN products p ON sa.product_id = p.id
        WHERE YEAR(sa.transaction_time) = %s AND MONTH(sa.transaction_time) = %s
        ORDER BY sa.transaction_time DESC
    """
    mycursor.execute(sql, (year, int(month)))
    rows = mycursor.fetchall()
    return [list(r) for r in rows]


def get_basic_list(table):
    """依表名抓出目前所有的 id, name 清單，由 id 排序"""
    sql = "SELECT id, name FROM %s ORDER BY id" % table
    mycursor.execute(sql)
    rows = mycursor.fetchall()
    return [list(r) for r in rows]


def insert_basic_data(table, name):
    """新增一筆基礎資料（廠商/貨品/顧客/幣別），id 交給資料庫 AUTO_INCREMENT"""
    name = (name or "").strip()
    if name:
        sql = "INSERT INTO %s (name) VALUES (%%s)" % table
        mycursor.execute(sql, (name,))
        mydb.commit()
        print(f"{table} 新增成功：{name}")
    # 不管有沒有新增成功，都回傳最新清單，順便清空輸入框
    return get_basic_list(table), ""


def insert_supplier(name):
    return insert_basic_data("suppliers", name)


def insert_product(name):
    return insert_basic_data("products", name)


def insert_customer(name):
    return insert_basic_data("customers", name)


def insert_currency(name):
    return insert_basic_data("currency", name)


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
        with gr.TabItem("出貨", id="sales"):
            with gr.Row():
                cus_dp = gr.Dropdown(choices=get_options("customers"),label="客戶",interactive=True)
                pro_dp2 = gr.Dropdown(choices=get_options("products"),label="貨品",interactive=True)
                qua2 = gr.Textbox(label="數量")
                unit_pri2 = gr.Textbox(label="單價")
                total2 = gr.Textbox(label="總額(可手動修改)")
                unit_dp2 = gr.Dropdown(choices=["公斤", "隻", "箱", "件"], value="公斤", label="計量單位",interactive=True)
                insert2 = gr.Button("新增")

            gr.Markdown("<h3 style='margin-top: 40px;'>出貨紀錄</h3>")
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
            )
        with gr.TabItem("進貨", id="purchases"):
            with gr.Row():
                sup_dp = gr.Dropdown(choices=get_options("suppliers"),label="廠商",interactive=True)
                pro_dp = gr.Dropdown(choices=get_options("products"),label="貨品",interactive=True)
                qua = gr.Textbox(label="數量")
                unit_pri = gr.Textbox(label="單價")
                total = gr.Textbox(label="總額(可手動修改)")
                # 新增商品或交易時，單位寫死常用選項，簡單又不會出錯
                unit_dp = gr.Dropdown(choices=["公斤", "隻", "箱", "件"], value="公斤", label="計量單位",interactive=True)
                Cur_dp = gr.Dropdown(choices=get_options("currency"),label="幣別",interactive=True)
                insert = gr.Button("新增")


            gr.Markdown("<h3 style='margin-top: 40px;'>進貨紀錄</h3>")
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
            )

        with gr.TabItem("基礎資料設定", id="basic_data"):
            with gr.Tabs():
                with gr.TabItem("廠商"):
                    with gr.Row():
                        sup_name = gr.Textbox(label="廠商名稱", scale=3)
                        sup_add_btn = gr.Button("新增廠商", scale=1)
                    sup_list = gr.Dataframe(
                        value=get_basic_list("suppliers"),
                        headers=["ID", "名稱"],
                        label="廠商清單",
                    )

                with gr.TabItem("貨品"):
                    with gr.Row():
                        pro_name = gr.Textbox(label="貨品名稱", scale=3)
                        pro_add_btn = gr.Button("新增貨品", scale=1)
                    pro_list = gr.Dataframe(
                        value=get_basic_list("products"),
                        headers=["ID", "名稱"],
                        label="貨品清單",
                    )

                with gr.TabItem("顧客"):
                    with gr.Row():
                        cus_name = gr.Textbox(label="顧客名稱", scale=3)
                        cus_add_btn = gr.Button("新增顧客", scale=1)
                    cus_list = gr.Dataframe(
                        value=get_basic_list("customers"),
                        headers=["ID", "名稱"],
                        label="顧客清單",
                    )

                with gr.TabItem("幣別"):
                    with gr.Row():
                        cur_name = gr.Textbox(label="幣別名稱", scale=3)
                        cur_add_btn = gr.Button("新增幣別", scale=1)
                    cur_list = gr.Dataframe(
                        value=get_basic_list("currency"),
                        headers=["ID", "名稱"],
                        label="幣別清單",
                    )

    # 使用 update 語法動態切換選取的頁籤 id

    insert.click(fn=insert_purchas, inputs=[sup_dp, pro_dp, qua, unit_pri, total, unit_dp, Cur_dp], outputs=purchases_table)
    refresh_btn.click(fn=get_purchases_table, inputs=[year_dp, month_dp], outputs=purchases_table)
# 當數量 (t1) 或單價 (t2) 改變時，自動去計算總額 (t3)
    qua.change(fn=auto_calculate_total, inputs=[qua, unit_pri], outputs=total)
    unit_pri.change(fn=auto_calculate_total, inputs=[qua, unit_pri], outputs=total)

    insert2.click(fn=insert_sale, inputs=[cus_dp, pro_dp2, qua2, unit_pri2, total2, unit_dp2], outputs=sales_table)
    refresh_btn2.click(fn=get_sales_table, inputs=[year_dp2, month_dp2], outputs=sales_table)
    qua2.change(fn=auto_calculate_total, inputs=[qua2, unit_pri2], outputs=total2)
    unit_pri2.change(fn=auto_calculate_total, inputs=[qua2, unit_pri2], outputs=total2)

    sup_add_btn.click(fn=insert_supplier, inputs=sup_name, outputs=[sup_list, sup_name])
    pro_add_btn.click(fn=insert_product, inputs=pro_name, outputs=[pro_list, pro_name])
    cus_add_btn.click(fn=insert_customer, inputs=cus_name, outputs=[cus_list, cus_name])
    cur_add_btn.click(fn=insert_currency, inputs=cur_name, outputs=[cur_list, cur_name])



try:
    demo.launch()
finally:
    mycursor.close()
    mydb.close()