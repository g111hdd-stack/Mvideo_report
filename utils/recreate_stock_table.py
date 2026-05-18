from config import DB_ARRIS_URL
from database.db import DbConnection


def main() -> None:
    db = DbConnection(url=DB_ARRIS_URL)
    try:
        # === DROP (от потомков к родителям) ===
        # db.drop_mv_advert_statistic()   # FK -> mv_adverts_table, clients
        # db.drop_mvideo_acquiring_table()          # FK -> clients
        # db.drop_mv_storage()
        # db.drop_mvideo_distribution_table()
        # db.drop_mv_stocks()
        # db.drop_mvideo_advert_table()
        # db.drop_mvideo_card_product_table()

        # === CREATE (от родителей к потомкам, clients уже должна быть в БД) ===
        # db.create_mv_card_product()
        # db.create_mv_adverts_table()
        # db.create_mv_statistic_advert()   # требует mv_adverts_table + clients
        # db.create_mv_stocks()
        # db.create_mv_log()
        # db.create_mv_acquiring()          # требует clients


        print("Все таблицы mvideo_* успешно пересозданы")
    finally:
        db.session.close()


if __name__ == "__main__":
    main()
