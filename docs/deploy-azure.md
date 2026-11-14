# Деплой на Azure App Service

Сайт: Web App **product-cart** (Linux, Python 3.12), план F1 (безкоштовно) для старту. Деплой — GitHub Actions (`.github/workflows/azure-deploy.yml`) на кожен push у `main`.

Потрібен [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (або те саме через портал) і права в підписці.

## 1. Ресурси (один раз)
```bash
az login
az group create --name product-cart-rg --location polandcentral
az appservice plan create --resource-group product-cart-rg --name product-cart-plan --is-linux --sku F1
az webapp create --resource-group product-cart-rg --plan product-cart-plan --name product-cart --runtime "PYTHON:3.12"
```
- Якщо в `polandcentral` немає F1 — візьміть `westeurope`.
- Якщо назва `product-cart` зайнята, Azure відмовить: візьміть іншу (наприклад `product-cart-ua`) і змініть `AZURE_WEBAPP_NAME` у workflow.
- Подивіться справжню адресу сайту — її треба вказати в `BASE_URL`:
```bash
az webapp show --resource-group product-cart-rg --name product-cart --query defaultHostName -o tsv
```

## 2. Налаштування застосунку
Згенеруйте секрет **на своєму комп'ютері** і нікому його не пересилайте:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
Підставте його і адресу з кроку 1:
```bash
az webapp config appsettings set --resource-group product-cart-rg --name product-cart --settings \
  APP_SECRET="<згенерований секрет>" \
  BASE_URL="https://<адреса з кроку 1>" \
  DB_PATH="/home/data/web.db" \
  SQLITE_JOURNAL="DELETE" \
  SCM_DO_BUILD_DURING_DEPLOYMENT="true"
az webapp config set --resource-group product-cart-rg --name product-cart --startup-file "bash startup.sh"
```
- `APP_SECRET` шифрує токени «Сільпо». Якщо його змінити, усім користувачам доведеться підключити «Сільпо» знову.
- `/home` у App Service зберігається між перезапусками, там лежить SQLite. `SQLITE_JOURNAL=DELETE` — бо `/home` це мережевий диск.

## 3. Доступ для GitHub Actions
Нові Web App за замовчуванням забороняють публікацію за профілем. Дозвольте її і завантажте профіль:
```bash
az resource update --resource-group product-cart-rg --name scm --namespace Microsoft.Web \
  --resource-type basicPublishingCredentialsPolicies --parent sites/product-cart --set properties.allow=true
az webapp deployment list-publishing-profiles --resource-group product-cart-rg --name product-cart --xml > publish-profile.xml
```
Відкрийте GitHub → репозиторій **product-cart** → Settings → Secrets and variables → Actions → **New repository secret**:
- Name: `AZURE_WEBAPP_PUBLISH_PROFILE`
- Secret: весь вміст `publish-profile.xml`

Після цього видаліть `publish-profile.xml` з диска.

## 4. Перший деплой
GitHub → Actions → **Deploy to Azure App Service** → Run workflow (або будь-який push у `main`). Після зеленого запуску відкрийте `https://<адреса>/`.

Логи застосунку:
```bash
az webapp log config --resource-group product-cart-rg --name product-cart --docker-container-logging filesystem
az webapp log tail --resource-group product-cart-rg --name product-cart
```

## Обмеження F1
60 хвилин процесора на добу, без Always On: після простою перший запит повільний (сайт «прокидається»). Для тесту з людьми — план B1 (~$13/міс):
```bash
az appservice plan update --resource-group product-cart-rg --name product-cart-plan --sku B1
```
