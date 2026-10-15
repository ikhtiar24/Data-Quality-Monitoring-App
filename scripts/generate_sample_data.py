"""Generate the reproducible demo CSV with realistic data-quality defects."""

import csv
import random
from datetime import date, timedelta
from pathlib import Path


def main():
    randomizer = random.Random(20261015)
    regions = ["North", "South", "East", "West"]
    first_names = ["Avery", "Jordan", "Morgan", "Taylor", "Riley", "Casey"]
    last_names = ["Patel", "Kim", "Garcia", "Miller", "Singh", "Brown"]
    start = date(2023, 1, 1)
    rows = []

    for number in range(1, 301):
        name = f"{randomizer.choice(first_names)}.{randomizer.choice(last_names)}"
        row = {
            "record_id": f"C{number:04d}",
            "customer_email": f"{name.lower()}{number}@example.com",
            "region": randomizer.choice(regions),
            "loyalty_score": round(randomizer.uniform(0, 100), 1),
            "order_total": round(randomizer.uniform(8, 850), 2),
            "signup_date": (start + timedelta(days=randomizer.randint(0, 1100))).isoformat(),
        }
        if number in {13, 48, 112, 197, 244}:
            row["customer_email"] = ""
        if number in {22, 89, 173, 261}:
            row["loyalty_score"] = 125.0
        if number in {37, 148, 219}:
            row["loyalty_score"] = ""
        if number == 65:
            row["customer_email"] = "not-an-email"
        if number == 101:
            row["region"] = "NORTH "
        if number == 151:
            row["customer_email"] = rows[149]["customer_email"]
        if number == 280:
            row["order_total"] = ""
        if number == 300:
            row = rows[-1].copy()
        rows.append(row)

    destination = Path(__file__).resolve().parents[1] / "sample_data.csv"
    with destination.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} sample rows to {destination}")


if __name__ == "__main__":
    main()
