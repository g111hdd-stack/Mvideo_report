from config import DB_ARRIS_URL
from database.db import DbConnection


def main() -> None:
    db = DbConnection(url=DB_ARRIS_URL)
    try:
        # === DROP (от потомков к родителям) ===
        # db.drop_mv_advert_statistic()   # FK -> mv_adverts_table, clients
        # db.drop_mv_main_table()         # FK -> clients
        # db.drop_mv_acquiring()          # FK -> clients
        # db.drop_mv_log()
        # db.drop_mv_distribution()
        # db.drop_mv_stocks()
        # db.drop_mv_adverts_table()
        # db.drop_mv_card_product()

        # === CREATE (от родителей к потомкам, clients уже должна быть в БД) ===
        # db.create_mv_card_product()
        # db.create_mv_adverts_table()
        # db.create_mv_statistic_advert()   # требует mv_adverts_table + clients
        # db.create_mv_stocks()
        # db.create_mv_log()
        # db.create_mv_acquiring()          # требует clients
        # db.create_mv_main_table()         # требует clients


        print("Все таблицы mvideo_* успешно пересозданы")
    finally:
        db.session.close()


if __name__ == "__main__":
    main()
