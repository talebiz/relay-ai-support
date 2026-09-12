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

یک‌بار در `support/views.py::SendMessageView` ساخته می‌شود و بدون تغییر در کل مسیر
جریان پیدا می‌کند: درخواست HTTP → command → ai-worker → همه‌ی eventهای آن اجرا →
(بعداً) Langfuse → SSE view که با همین id فیلتر می‌کند کدام رویداد مال کدام اتصال
مرورگر است → React.

### پخش زنده (SSE)

`streaming/views.py::conversation_stream` یک async view روی ASGI است. هر اتصال SSE
صف اختصاصی و auto-delete خودش را می‌سازد و به exchange رویدادها بایند می‌کند — دقیقاً
مطابق الزام «هر اتصال اشتراک کوتاه‌عمر خودش را داشته باشد».

این view هیچ حافظه‌ای از قبل از باز شدن اتصال ندارد؛ صرفاً یک رله‌ی زنده است. اگر تب
مرورگر وسط جریان قطع و وصل شود، فقط رویدادهای بعد از reconnect را می‌بیند. منبع حقیقتِ
«واقعاً چه پاسخی تولید شد» پایگاه‌داده است، نه استریم SSE؛ وظیفه‌ی فرانت‌اند در
reconnect این است که وضعیت گفتگو را از یک endpoint REST متکی بر دیتابیس دوباره بخواند
و بعد به شنیدن جریان زنده ادامه دهد.

### محدودیت‌های شناخته‌شده‌ی این مرحله

- مدل‌های `Conversation`/`Message` هنوز ساخته نشده‌اند؛ `support/views.py` فعلاً هر
  درخواست را با یک `correlation_id` تازه منتشر می‌کند.
- `ai-worker/worker.py::run_agent_placeholder` یک echo ساده است؛ عامل واقعی LangGraph
  در بخش ۴ جایگزین آن می‌شود، بدون نیاز به تغییر منطق idempotency/retry/DLQ اطراف آن.
- بررسی مالکیت `correlation_id` در `streaming/views.py` هنوز پیاده نشده و به‌صراحت
  TODO است.
- محدودیت idempotency بالاتر («فقط دیتابیس را پوشش می‌دهد») هنوز به‌طور کامل حل
  نشده.