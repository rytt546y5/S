"""Run once while the bot is STOPPED to discard the old PayPay login only."""

import os

from dotenv import load_dotenv

from storage import clear_paypay_login, initialize


def main() -> None:
    load_dotenv()
    raw = os.getenv('OWNER_ID', '').strip()
    if not raw.isdigit():
        raise SystemExit('OWNER_ID を .env に設定してください。')
    initialize()
    count = clear_paypay_login(int(raw))
    print('既存のPayPay認証情報を初期化しました。 /paypay_login からログインしてください。')
    print('対象件数:', count)
    print('注文履歴、SMS注文、設定、暗号化鍵は変更していません。')


if __name__ == '__main__':
    main()
