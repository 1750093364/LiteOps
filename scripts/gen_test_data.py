# -*- coding: utf-8 -*-
"""生成卡01自测数据（可重复运行，覆盖生成）。

输出到 data/test/：
1. sales_orders_50k.csv   5万行订单数据（含三种日期格式混用、千分位金额、
                          异常年龄、缺失渠道、200 行完全重复）
2. activity_small.xlsx    约 200 行活动数据（city 首尾空格、phone 3 个位数错误）
3. gbk_orders_1k.csv      1 千行 GBK 编码样本（用于编码识别/手动切换自测）

加 --big 参数时额外生成：
4. sales_orders_1m.csv    100 万行订单数据（998,000 唯一行 + 2,000 完全重复行，
                          用于卡13 性能实测：导入/体检/清洗）

用法：.venv\\Scripts\\python.exe scripts\\gen_test_data.py [--big]
"""
import csv
import random
import sys
from pathlib import Path

from openpyxl import Workbook

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "test"

random.seed(2026)  # 固定种子保证可复现

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京", "西安", "重庆"]
CHANNELS = ["线上", "线下", "APP", "小程序"]


def rand_date_parts():
    y, m = 2026, random.randint(1, 8)
    d = random.randint(1, 28)
    return y, m, d


def fmt_date(y, m, d):
    style = random.randint(0, 2)
    if style == 0:
        return f"{y}/{m}/{d}"
    if style == 1:
        return f"{y}-{m:02d}-{d:02d}"
    return f"{y}年{m}月{d}日"


def _order_row(i: int) -> list[str]:
    """生成第 i 行订单记录（字符串字段，含预设比例的脏数据）。"""
    y, m, d = rand_date_parts()
    order_date = fmt_date(y, m, d)

    r = random.random()
    if r < 0.01:                                # 约 1% 负数金额
        amount = f"-{random.uniform(10, 800):.2f}"
    elif r < 0.06:                              # 约 5% 千分位文本
        amount = f"{random.uniform(1000, 99999):,.1f}"
    else:
        amount = f"{random.uniform(10, 5000):.2f}"

    a = random.random()
    if a < 0.005:                               # 约 0.5% 年龄 <0
        age = str(random.randint(-9, -1))
    elif a < 0.01:                              # 约 0.5% 年龄 >120
        age = str(random.randint(121, 150))
    else:
        age = str(random.randint(18, 65))

    channel = "" if random.random() < 0.05 else random.choice(CHANNELS)  # 约 5% 缺失

    return [
        str(i),                                  # order_id
        str(random.randint(10000, 99999)),       # user_id
        order_date,
        amount,
        age,
        random.choice(CITIES),                   # city
        channel,
    ]


def gen_order_rows(n_unique: int):
    """生成 n_unique 行订单记录（列表，每行为字符串字段）。"""
    return [_order_row(i) for i in range(1, n_unique + 1)]


def gen_sales_csv():
    """sales_orders_50k.csv：49800 唯一行 + 200 完全重复行 = 50000 行。"""
    unique_rows = gen_order_rows(49800)
    dup_rows = random.sample(unique_rows, 200)
    all_rows = unique_rows + dup_rows
    random.shuffle(all_rows)

    path = OUT_DIR / "sales_orders_50k.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "user_id", "order_date", "amount", "age", "city", "channel"])
        w.writerows(all_rows)
    return path, len(all_rows)


def gen_activity_xlsx():
    """activity_small.xlsx：约 200 行，city 首尾空格、phone 3 个位数错误。"""
    names = ["张伟", "王芳", "李娜", "刘洋", "陈静", "杨帆", "赵磊", "黄敏", "周杰", "吴霞"]
    rows = []
    for i in range(1, 201):
        city = random.choice(CITIES)
        if random.random() < 0.35:                  # 约 35% 行首尾空格
            city = f"  {city} "
        phone = "1" + random.choice("3589") + "".join(random.choices("0123456789", k=9))
        y, m, d = rand_date_parts()
        rows.append([str(i), random.choice(names), city, phone, f"{y}-{m:02d}-{d:02d}"])

    # 3 个位数错误的手机号：第 10 / 50 / 120 行（1-based）
    for idx, bad in ((10, "1380013800"), (50, "1591234567890"), (120, "1860099887")):
        rows[idx - 1][3] = bad

    wb = Workbook()
    ws = wb.active
    ws.title = "活动名单"
    ws.append(["id", "name", "city", "phone", "signup_date"])
    for row in rows:
        ws.append(row)
    path = OUT_DIR / "activity_small.xlsx"
    wb.save(path)
    return path, len(rows)


def gen_gbk_csv():
    """gbk_orders_1k.csv：1 千行 GBK 编码样本（含中文，验证编码识别与手动切换）。"""
    rows = gen_order_rows(1000)
    path = OUT_DIR / "gbk_orders_1k.csv"
    with open(path, "w", newline="", encoding="gbk") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "user_id", "order_date", "amount", "age", "city", "channel"])
        w.writerows(rows)
    return path, len(rows)


def gen_sales_csv_big(n_total: int = 1_000_000, n_dup: int = 2000):
    """sales_orders_1m.csv：100 万行（998,000 唯一 + 2,000 完全重复）。

    流式分批生成写入（每批 5 万行），内存占用 O(批大小)，不整表驻留。
    重复行从保留的样本缓冲（每 250 行留样 1 行，凑满 n_dup 个）中抽取。
    """
    n_unique = n_total - n_dup
    path = OUT_DIR / "sales_orders_1m.csv"
    BATCH = 50_000
    keep_every = max(1, n_unique // n_dup)   # 留样间隔：保证凑够 n_dup 个样本
    dup_pool: list[list[str]] = []
    written = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "user_id", "order_date", "amount", "age", "city", "channel"])
        batch: list[list[str]] = []
        for i in range(1, n_unique + 1):
            batch.append(_order_row(i))
            if i % keep_every == 0 and len(dup_pool) < n_dup:
                dup_pool.append(batch[-1])
            if len(batch) >= BATCH:
                w.writerows(batch)
                written += len(batch)
                batch.clear()
        # 末尾补 2,000 行完全重复（连同残余未满批的唯一行一并落盘）
        random.shuffle(dup_pool)
        batch.extend(dup_pool)
        w.writerows(batch)
        written += len(batch)
    return path, written


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p1, n1 = gen_sales_csv()
    p2, n2 = gen_activity_xlsx()
    p3, n3 = gen_gbk_csv()
    print(f"[OK] {p1.name}  {n1} 行  {p1.stat().st_size / 1024:.0f} KB")
    print(f"[OK] {p2.name}  {n2} 行  {p2.stat().st_size:.0f} B")
    print(f"[OK] {p3.name}  {n3} 行  {p3.stat().st_size / 1024:.0f} KB（GBK 编码自测用）")
    if "--big" in sys.argv:
        p4, n4 = gen_sales_csv_big()
        print(f"[OK] {p4.name}  {n4} 行  {p4.stat().st_size / 1048576:.0f} MB（性能实测用）")


if __name__ == "__main__":
    main()
