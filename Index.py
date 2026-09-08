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


def get_available_year_months():
    """從 purchases 表抓出所有『真的有資料』的年月組合，由新到舊排序"""
    sql = """
        SELECT DISTINCT YEAR(time) AS y, MONTH(time) AS m
        FROM purchases
        WHERE time IS NOT NULL
        ORDER BY y DESC, m DESC
    """
    mycursor.execute(sql)
    rows = mycursor.fetchall()
    # 回傳格式例如 [(2026, 8), (2026, 6), (2025, 12), ...]
    return [(row[0], row[1]) for row in rows]


def get_purchases_table(year, month):
    """依年月查詢進貨紀錄，回傳純資料（list of lists）給表格顯示"""
    sql = """
        SELECT pu.time, s.name AS 廠商, p.name AS 貨品,
               pu.quantity AS 數量, pu.unit AS 單位,
               pu.unit_price AS 單價, pu.total_price AS 總額,
               cur.name AS 幣別
        FROM purchases pu
        LEFT JOIN suppliers s ON pu.supplier_id = s.id
        LEFT JOIN products p ON pu.product_id = p.id
        LEFT JOIN currency cur ON pu.currency_id = cur.id
        WHERE YEAR(pu.time) = %s AND MONTH(pu.time) = %s
        ORDER BY pu.time DESC
    """
    mycursor.execute(sql, (year, int(month)))
    rows = mycursor.fetchall()
    return [list(r) for r in rows]


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
        with gr.TabItem("進貨", id="purchases"):
            with gr.Row():
                sup_dp = gr.Dropdown(choices=get_options("suppliers"),label="廠商",interactive=True)
                pro_dp = gr.Dropdown(choices=get_options("products"),label="貨品",interactive=True)
                qua = gr.Textbox(label="數量")
                unit_pri = gr.Textbox(label="單價")
                total = gr.Textbox(label="總額(可手動修改)")
                # 新增商品或交易時，單位寫死常用選項，簡單又不會出錯
                unit_dp = gr.Dropdown(choices=["公斤", "隻", "箱", "件"], value="公斤", label="計量單位",interactive=True)
                Cur_dp = gr.Dropdown(choices=get_options("Currency"),label="幣別",interactive=True)
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

        with gr.TabItem("出貨", id="sales"):
            gr.Markdown("這是第二頁的內容")
            #btn2 = gr.Button("新增")

    # 使用 update 語法動態切換選取的頁籤 id

    insert.click(fn=insert_purchas, inputs=[sup_dp, pro_dp, qua, unit_pri, total, unit_dp, Cur_dp], outputs=purchases_table)
    refresh_btn.click(fn=get_purchases_table, inputs=[year_dp, month_dp], outputs=purchases_table)
# 當數量 (t1) 或單價 (t2) 改變時，自動去計算總額 (t3)
    qua.change(fn=auto_calculate_total, inputs=[qua, unit_pri], outputs=total)
    unit_pri.change(fn=auto_calculate_total, inputs=[qua, unit_pri], outputs=total)



try:
    demo.launch()
finally:
    mycursor.close()
    mydb.close()