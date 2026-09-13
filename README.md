# Relay — AI-Powered Customer Support Assistant

نام: محمدپارسا طالبی زاده

## بخش ۳ — معماری رویدادمحور با RabbitMQ

### چرا این بخش مهم‌ترین تفاوت با دوره است

در دوره‌ی مرجع، عامل داخل همان چرخه‌ی درخواست/پاسخ Django اجرا می‌شود و یک worker وب
را برای کل مدت اجرا (چند ثانیه) اشغال می‌کند. در Relay، لایه‌ی وب و لایه‌ی اجرای هوش
مصنوعی (`ai-worker`) دو پردازه‌ی جدا هستند که فقط از طریق RabbitMQ حرف می‌زنند:

```
React → Django (وب) --command--> RabbitMQ --> ai-worker (عامل)
                                                    |
Django (SSE) <--event-- RabbitMQ <-----------------+
```

`RabbitMQ` تنها پل بین backend و ai-worker است. PostgreSQL و ChromaDB منابع مشترکی
هستند که هر دو سرویس مستقیماً به آن‌ها متصل می‌شوند.

### Command در برابر Event

| | Command | Event |
|---|---|---|
| معنی | «این کار را انجام بده» | «این اتفاق افتاد» |
| مصرف‌کننده | دقیقاً یک (ai-worker) | صفر یا چند |
| مسیر | صف `agent_commands` | exchange نوع fanout (`agent_events_exchange`) |
| خطا | نیازمند retry/backoff/DLQ | عدم تحویل لزوماً خطا نیست |

پیاده‌سازی این تمایز در `messaging/envelope.py` (`MessageKind`) و
`messaging/topology.py` است: صف command دقیقاً یک consumer دارد، اما رویدادها به یک
exchange فن‌اوت می‌روند و هر اتصال SSE صف اختصاصی و کوتاه‌عمر خودش را می‌سازد — چون
ناشر نباید بداند چه کسی گوش می‌دهد.

**Commandها:** `command.process_user_message`

**Eventها:** `agent_run_started`, `agent_response_chunk`, `agent_tool_called`,
`agent_run_completed`, `agent_run_failed`

### Envelope مشترک

هر پیام (command یا event) این شکل را دارد (`messaging/envelope.py::Envelope`):

```json
{
  "message_id": "UUID - کلید idempotency، مخصوص همین پیام روی سیم",
  "correlation_id": "UUID - مشترک بین کل پیام‌های یک تعامل کاربر",
  "kind": "command | event",
  "message_type": "مثلاً command.process_user_message",
  "schema_version": 1,
  "occurred_at": "ISO-8601 UTC",
  "producer": "backend | ai-worker",
  "retry_count": 0,
  "payload": { "...": "..." }
}
```

`message_id` و `correlation_id` دو مفهوم متفاوتند: یک command می‌تواند چند event
تولید کند که همه یک `correlation_id` دارند اما `message_id` های جدا.

این فایل عمداً هم در `backend/messaging/envelope.py` و هم در `ai-worker/envelope.py`
تکرار (نه import) شده، چون این دو سرویس دو ایمیج مستقل با dependency جدا هستند و
اشتراک کد بین‌شان پیچیدگی عملیاتی غیرضروری اضافه می‌کند. اگر شکل envelope تغییر کند،
باید دستی در هر دو جا هماهنگ شود.

### قابلیت اطمینان (Reliability)

**Manual ack:** `ai-worker/worker.py` هرگز auto-ack نمی‌کند؛ `basic_qos(prefetch_count=1)`
تضمین می‌کند پیام بعدی تا ack نشدن پیام فعلی تحویل داده نشود.

**Retry با backoff نمایی:** به‌جای پلاگین اضافه، از الگوی Dead Letter Exchange + صف‌های
TTL استفاده شده (`messaging/topology.py`): سه صف retry با TTL ۱، ۴ و ۱۶ ثانیه که با
انقضای پیام، آن را به صف اصلی برمی‌گردانند. بعد از اتمام retryها، پیام مستقیم به
`agent_commands.dlq` می‌رود و یک `agent_run_failed` هم منتشر می‌شود.

**Idempotency در دیتابیس:** جدول `messaging_processedmessage` با constraint یکتا روی
`message_id`. این تضمین با دو تابع جدا در `ai-worker/db.py` پیاده شده، که در دو نقطه‌ی
متفاوت از `worker.py::handle_command` صدا زده می‌شوند:

1. `already_processed(message_id)` — یک `SELECT` ارزان، **قبل از** اجرای عامل. اگر
   این پیام قبلاً کامل پردازش شده (رایج‌ترین حالت redelivery)، worker بدون اجرای
   دوباره‌ی عامل و بدون انتشار دوباره‌ی هیچ eventی، بلافاصله ack می‌کند.
2. `mark_processed(message_id, ...)` — `INSERT` واقعی، **بعد از** اتمام کامل کار عامل
   (و انتشار همه‌ی eventهایش)، درست قبل از ack. اگر دو تحویل هم‌زمان از چک اول رد شده
   باشند (race نادر)، فقط یکی از این دو `INSERT` موفق می‌شود؛ دیگری با
   `DuplicateMessageError` مواجه شده و بدون کار اضافه ack می‌شود.

این دو مرحله عمداً جدا از هم و در دو زمان متفاوت هستند، نه در یک تراکنش واحد دور کل
کار عامل: انتشار event روی RabbitMQ و نوشتن در PostgreSQL دو سیستم مجزا هستند که
نمی‌توان آن‌ها را در یک commit اتمی واحد بست (بدون یک الگوی پیشرفته‌تر مثل
transactional outbox که برای این پروژه خارج از محدوده است). نوشتن ثبت نهایی به‌عنوان
آخرین قدم ممکن، پنجره‌ی خطر را تا حد زیادی کوچک می‌کند اما کاملاً حذفش نمی‌کند — به
بخش «محدودیت شناخته‌شده» زیر نگاه کنید.

#### پاسخ به دو پرسش الزامی سند

**اگر همان فرمان دو بار برسد چه می‌شود؟**
تضمین واقعی RabbitMQ «حداقل یک‌بار تحویل» است، نه دقیقاً یک‌بار. در رایج‌ترین حالت،
`already_processed` این را زودهنگام تشخیص می‌دهد و worker بدون اجرای دوباره‌ی هیچ
کاری ack می‌کند. در حالت نادرتر (race بین دو تحویل هم‌زمان)، محدودیت یکتای دیتابیس در
`mark_processed` تضمین می‌کند فقط یکی به‌عنوان «پردازش‌شده» ثبت شود.

**اگر worker پیش از تأیید از کار بیفتد چه می‌شود؟**
چون ack فقط دستی و بعد از موفقیت کامل فرستاده می‌شود، RabbitMQ در صورت کرش worker
پیام را دوباره تحویل می‌دهد. اگر کرش **بعد از** `mark_processed` باشد، تحویل دوباره
کاملاً بی‌خطر است. اگر کرش **بین انتشار eventها و `mark_processed`** باشد، محدودیتی
واقعی باقی می‌ماند که در ادامه توضیح داده شده.

#### محدودیت شناخته‌شده

اگر worker دقیقاً بعد از انتشار موفق eventهای عامل (که کاربر آن‌ها را در SSE دیده) ولی
قبل از موفقیت `mark_processed` کرش کند، eventها قابل بازگشت نیستند اما دیتابیس هنوز
نمی‌داند این پیام پردازش شده؛ تحویل دوباره باعث اجرای مجدد عامل و انتشار دوباره‌ی همان
eventها می‌شود — یعنی کاربر ممکن است پاسخ را دو بار در SSE ببیند. راه‌حل کامل این مورد
(transactional outbox) پیچیدگی عملیاتی بیشتری نیاز دارد که برای اندازه‌ی این پروژه
توجیه‌پذیر نیست؛ تصمیم گرفته شد این پنجره‌ی خطر را با ترتیب‌دهی دقیق تا حد امکان کوچک
نگه داریم و صریح مستندش کنیم، نه این‌که ادعا کنیم کاملاً حل شده.

### correlation_id

یک‌بار در `conversations/models.py::Conversation.correlation_id` ساخته می‌شود (وقتی
مکالمه‌ی مربوط به یک سفارش برای اولین‌بار ساخته می‌شود) و **بدون تغییر برای کل عمر آن
مکالمه** جریان پیدا می‌کند: هر پیام کاربر → command → ai-worker → همه‌ی eventهای آن
اجرا → (بعداً) Langfuse → SSE view که با همین id فیلتر می‌کند کدام رویداد مال کدام
اتصال مرورگر است → React. (در بخش ۳ این id به‌صورت موقت هر درخواست را تازه تولید
می‌کرد؛ این محدودیت در بخش ۴ commit ۱ برطرف شد — به پایین‌تر نگاه کنید.)

### پخش زنده (SSE)

`streaming/views.py::conversation_stream` یک async view روی ASGI است. هر اتصال SSE
صف اختصاصی و auto-delete خودش را می‌سازد و به exchange رویدادها بایند می‌کند — دقیقاً
مطابق الزام «هر اتصال اشتراک کوتاه‌عمر خودش را داشته باشد».

این view هیچ حافظه‌ای از قبل از باز شدن اتصال ندارد؛ صرفاً یک رله‌ی زنده است. اگر تب
مرورگر وسط جریان قطع و وصل شود، فقط رویدادهای بعد از reconnect را می‌بیند. منبع حقیقتِ
«واقعاً چه پاسخی تولید شد» پایگاه‌داده است (`conversations.Message`، بخش ۴)، نه استریم
SSE؛ وظیفه‌ی فرانت‌اند در reconnect این است که تاریخچه‌ی گفتگو را از
`GET /api/conversations/<id>/messages/` (یک endpoint REST متکی بر دیتابیس) دوباره
بخواند و بعد به شنیدن جریان زنده ادامه دهد.

---

## بخش ۴ (Commit ۱) — زیرساخت پایه: مکالمات، LLM Factory، تولید پاسخ واقعی بدون ابزار

این commit سه محدودیتی که در پایان بخش ۳ صریحاً به‌عنوان TODO ثبت شده بودند را حل
می‌کند، و زیرساخت لازم برای گراف واقعی LangGraph (commit ۲) را آماده می‌کند — بدون
این‌که هنوز خود گراف یا ابزارهایش وجود داشته باشند.

### ۱. اپ `conversations` (Django) — منبع حقیقتِ تاریخچه‌ی چت

دو مدل در `backend/conversations/models.py`:

- **`Conversation`**: دقیقاً یک مکالمه به‌ازای هر سفارش (`order` یک `OneToOneField`
  است)، با `correlation_id` یکتا که یک‌بار ساخته و برای همیشه استفاده می‌شود.
- **`Message`**: هر پیام (`role`: user/assistant)، با فیلد اختیاری
  `tool_calls_json` که هنوز خالی می‌ماند (برای commit ۲، وقتی ابزار واقعی وجود داشته
  باشد و نمایش «فعالیت عامل» در UI لازم شود).

`conversations/services.py` دقیقاً همان الگوی `orders/services.py` را تکرار می‌کند:
تنها نقطه‌ی مجاز برای پرس‌وجوی `Conversation`/`Message`، با قاعده‌ی «۴۰۴ نه ۴۰۳» و
پشتیبان‌ها به همه‌چیز دسترسی دارند. نکته‌ی مهم: مالک یک مکالمه همیشه **صاحب سفارش**
است، نه کاربری که آن را trigger کرده — اگر پشتیبان سفارش باب را باز کند، مکالمه به نام
باب ساخته می‌شود، نه پشتیبان.

Endpointها:
- `GET/POST /api/orders/<order_id>/conversation/` → fetch-or-create
- `GET /api/conversations/<id>/messages/` → resync تاریخچه (برای reconnect SSE)

### ۲. `support/views.py` — پایدارسازی `correlation_id`

`SendMessageView` دیگر هر بار یک `correlation_id` تصادفی نمی‌سازد. حالا:

1. مکالمه را از طریق `conversations.services.get_conversation_for_user` پیدا می‌کند
   (چک مالکیت — دقیقاً همان chokepoint بقیه‌ی پروژه).
2. پیام کاربر را **همان لحظه، به‌صورت sync** در دیتابیس ذخیره می‌کند — قبل از این‌که
   هیچ commandای منتشر شود.
3. command را با `conversation.correlation_id` منتشر می‌کند (پایدار برای کل مکالمه).

### ۳. `streaming/views.py` — رفع TODO مالکیت

قبل از باز کردن اتصال SSE، توکن JWT از query param (`?token=...`) خوانده می‌شود (چون
`EventSource` نمی‌تواند header بفرستد)، کاربر resolve می‌شود، و بعد
`conversations.services.get_conversation_for_user_by_correlation_id` چک می‌کند این
کاربر واقعاً مالک همین `correlation_id` هست یا نه. اگر نه، بدون باز کردن استریم، ۴۰۱ یا
۴۰۴ برمی‌گردد.

### ۴. `ai-worker/llm/factory.py` — انتزاع LLM با fallback خودکار

- `LLMProvider`: سه حالت — `openai_compatible` (اکثر سرویس‌های رایگان مثل Groq و
  OpenRouter، چون API شبیه OpenAI دارند)، `anthropic`، `google_genai`.
- `build_chat_model(config)` تنها جایی است که کتابخانه‌ی اختصاصی هر provider را
  import می‌کند؛ بقیه‌ی کد (از جمله گراف عامل در commit ۲) فقط با `BaseChatModel`
  کار می‌کند.
- `build_model_with_fallback(configs)` با `.with_fallbacks()` خودِ LangChain، مدل دوم
  را در صورت شکست مدل اول (rate limit، timeout، خطای provider) خودکار جایگزین می‌کند
  — همان بخش امتیازی «بازگشت خودکار به مدل دوم».
- پیکربندی کاملاً از env می‌آید (`LLM_PRIMARY_*` / `LLM_FALLBACK_*` در `.env.example`)؛
  عوض‌کردن provider یا مدل نیاز به تغییر کد ندارد.

### ۵. `ai-worker/db.py` و `worker.py` — پاسخ واقعی، هنوز بدون ابزار

`db.py` دو تابع جدید دارد که مستقیم روی جدول `conversations_message` (ساخته‌شده
توسط migration جنگو) کار می‌کنند، چون worker یک برنامه‌ی جنگو نیست:
`fetch_conversation_history` (خواندن تاریخچه برای ساخت prompt) و
`save_assistant_message` (نوشتن پاسخ نهایی — منبع حقیقت پاسخ).

`worker.py::run_agent_placeholder` (اکو ساده‌ی بخش ۳) با `run_agent` جایگزین شد:
تاریخچه از دیتابیس خوانده می‌شود، به مدل زبانی (از طریق `llm/factory.py`) داده
می‌شود، پاسخ به‌صورت **stream** گرفته می‌شود ولی رویدادهای `agent_response_chunk`
تنها هر ~۴۰ کاراکتر یک‌بار منتشر می‌شوند (نه به‌ازای هر token — الزام صریح بخش ۶
سند)، و پاسخ نهایی در `Message` ذخیره می‌شود. عامل **هنوز هیچ ابزاری صدا نمی‌زند** و
فقط بر پایه‌ی تاریخچه‌ی مکالمه پاسخ می‌دهد؛ اگر سؤال نیاز به دیتابیس/RAG/وب داشته
باشد، طبق system prompt صادقانه می‌گوید که این قابلیت هنوز اضافه نشده. منطق
idempotency/retry/DLQ اطراف آن (بخش ۳) دست‌نخورده ماند.

### محدودیت‌های شناخته‌شده‌ی این commit

- عامل هنوز **هیچ ابزاری** ندارد (نه دیتابیس سفارش، نه RAG، نه جست‌وجوی وب، نه ثبت
  بازگشت وجه) — فقط یک فراخوانی تک‌مرحله‌ای LLM بر پایه‌ی تاریخچه‌ی مکالمه. گراف
  LangGraph و ۴ ابزار در commit ۲ اضافه می‌شوند.
- هنوز `Checkpointer`ی برای LangGraph وجود ندارد چون گراف واقعی هنوز ساخته نشده؛
  در commit ۲ از `MemorySaver` استفاده خواهد شد (نه Postgres checkpointer)، چون
  تاریخچه‌ی واقعی مکالمه از `conversations.Message` می‌آید، نه از حافظه‌ی گراف —
  گراف فقط باید state یک اجرا را نگه دارد.
- فرانت‌اند React هنوز به‌روزرسانی نشده (صفحه‌ی سفارش/چت/SSE) — commit ۳.
- بافر رویدادهای تدریجی بر اساس تعداد کاراکتر است، نه توکن دقیق مدل؛ برای این اندازه
  پروژه کافی است اما شمارش دقیق token (برای بخش ۷، رصدپذیری هزینه) هنوز اضافه نشده.

### محدودیت‌های شناخته‌شده‌ی این مرحله (بخش ۳، هنوز باز)

- `ai-worker/worker.py::run_agent` هنوز echo نیست ولی هنوز عامل واقعی LangGraph با
  ابزار نیست؛ commit ۲ آن را جایگزین می‌کند بدون نیاز به تغییر منطق
  idempotency/retry/DLQ اطراف آن.
