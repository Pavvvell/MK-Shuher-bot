from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    telegram_bot_token: str
    telegram_webhook_secret: str
    public_base_url: str
    openai_api_key: str
    openai_model: str = "gpt-4.1-mini"
    database_url: str = ""
    cloud_sql_instance: str = ""
    db_user: str = ""
    db_password: str = ""
    db_name: str = "shuher"
    spreadsheet_id: str = "1ZSmctpTVh5NsCa4pW0acpo6CZGrddhOj9xjmTlsB03c"
    google_service_account_json_b64: str = ""
    owner_ids: str = ""
    buyer_ids: str = ""
    storekeeper_ids: str = ""
    # Switch ONLY after import counts and operations have been validated.
    data_backend: str = "sheets"
    import_source_spreadsheet_id: str = ""
    enable_mini_app: bool = False
    register_telegram_webhook: bool = True

    @staticmethod
    def parse_ids(value: str) -> set[int]:
        return {int(x.strip()) for x in value.split(",") if x.strip()}

    def role(self, user_id: int) -> str | None:
        if user_id in self.parse_ids(self.owner_ids):
            return "owner"
        if user_id in self.parse_ids(self.buyer_ids):
            return "buyer"
        if user_id in self.parse_ids(self.storekeeper_ids):
            return "storekeeper"
        return None
