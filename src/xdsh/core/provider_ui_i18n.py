"""Provider setup copy; no translated strings participate in authorization."""

from xdsh.core.i18n import canonical_language, language_codes

_KEYS = ("catalog", "custom", "intro", "unavailable", "private", "busy", "unsupported",
         "expired", "cancelled", "invalid", "confirm", "saved", "failed",
         "step0", "step1", "step2", "step3", "step4")
_ROWS = {
    "en": (
        "Provider setup catalog. ⚙️ Configurable API; not verified. 🔒 Protocol/authentication not bridged. Only confirmed connections appear in /model.", "Custom",
        "Inputs configure the connection, not an agent task. /cancel cancels; expires in 10 minutes.", "Configure XDSH_PROVIDERS_ROOT and a managed gateway first.", "Use the Bot private chat for provider setup.", "Setup is busy; retry later.", "This protocol/authentication is not bridged. No credentials were read and no login started. Use /model add with a compatible custom gateway.",
        "Setup or approval expired. Nothing saved; use /model add again.", "Cancelled; no new connection saved.", "Invalid input. Check the required format and try again.", "Confirm saving and allowing model requests and the corresponding credentials to this endpoint. The key is not echoed; its original Telegram message may remain.", "Connection saved. Use /model to select it. Saved does not mean verified: model, tool and reasoning support depend on the upstream.", "Connection not saved. Check for duplicate IDs or invalid configuration.",
        "Provider ID (e.g. deepseek, local):", "Protocol: anthropic / openai / gemini / azure:API_VERSION", "Base URL: HTTPS or an explicit loopback IP for local models:", "Exact upstream model ID; no wildcard:", "Credential: env:VARIABLE_NAME or API key. The key is not echoed:",
    ),
    "zh-TW": (
        "模型接入目錄。⚙️ 可設定 API，但尚未驗證。🔒 協議或認證尚未橋接。確認保存後才出現在 /model。", "自訂",
        "輸入只用於連線設定，不交給 Agent。/cancel 取消；10 分鐘到期。", "請先設定 XDSH_PROVIDERS_ROOT 與自有模型網關。", "請在 Bot 私聊設定模型接入。", "設定服務忙碌，請稍後再試。", "此協議或認證尚未橋接。未讀取憑據、未啟動登入；可用 /model add 接入自有相容網關。",
        "設定或確認已到期，未保存；請重新使用 /model add。", "已取消，未保存新增連線。", "輸入無效，請核對所需格式再試。", "確認後保存，並允許向此地址發送模型請求與對應憑據。Key 不回顯；Telegram 中的原訊息可能仍保留。", "連線已保存，請用 /model 選擇。保存不代表已驗證；模型、工具與推理支援取決於上游。", "連線未保存，請檢查 ID 是否重複或設定是否無效。",
        "提供商 ID，例如 deepseek、local：", "協議：anthropic / openai / gemini / azure:API_VERSION", "基礎 URL：HTTPS，或本地模型的明確 loopback IP：", "上游精確模型 ID，不使用萬用字元：", "憑據：env:VARIABLE_NAME 或 API Key；不回顯 Key：",
    ),
    "zh-CN": (
        "模型接入目录。⚙️ 可配置 API，但尚未验证。🔒 协议或认证尚未桥接。确认保存后才出现在 /model。", "自定义",
        "输入只用于连接配置，不交给 Agent。/cancel 取消；10 分钟到期。", "请先配置 XDSH_PROVIDERS_ROOT 与自有模型网关。", "请在 Bot 私聊配置模型接入。", "配置服务忙碌，请稍后重试。", "此协议或认证尚未桥接。未读取凭据、未启动登录；可用 /model add 接入自有兼容网关。",
        "配置或确认已到期，未保存；请重新使用 /model add。", "已取消，未保存新增连接。", "输入无效，请核对所需格式重试。", "确认后保存，并允许向此地址发送模型请求与对应凭据。Key 不回显；Telegram 中的原消息可能仍保留。", "连接已保存，请用 /model 选择。保存不代表已验证；模型、工具与推理支持取决于上游。", "连接未保存，请检查 ID 是否重复或配置是否无效。",
        "提供商 ID，例如 deepseek、local：", "协议：anthropic / openai / gemini / azure:API_VERSION", "基础 URL：HTTPS，或本地模型的明确 loopback IP：", "上游精确模型 ID，不使用通配符：", "凭据：env:VARIABLE_NAME 或 API Key；不回显 Key：",
    ),
    "ja": (
        "接続設定一覧。⚙️ API 設定可能、未検証。🔒 プロトコル・認証未対応。保存を確認した接続のみ /model に表示します。", "カスタム",
        "入力は接続設定専用で、エージェントには渡しません。/cancel で取消、10分で期限切れ。", "先に XDSH_PROVIDERS_ROOT と管理ゲートウェイを設定してください。", "Bot との個人チャットで設定してください。", "設定が混雑しています。後で再試行してください。", "このプロトコル・認証には未対応です。認証情報の読取りやログインは行いません。/model add で互換ゲートウェイを設定できます。",
        "設定または確認の期限が切れました。未保存です。/model add でやり直してください。", "取り消しました。新しい接続は保存していません。", "入力が無効です。必要な形式を確認してください。", "保存し、この接続先へのモデル要求と認証情報の送信を許可しますか？キーは再表示しませんが、Telegram の元のメッセージは残る場合があります。", "接続を保存しました。/model で選択できます。保存は検証済みを意味しません。モデル・ツール・推論の対応は接続先によります。", "接続を保存できませんでした。ID の重複や設定を確認してください。",
        "プロバイダー ID（deepseek、local など）：", "プロトコル：anthropic / openai / gemini / azure:API_VERSION", "基本 URL：HTTPS またはローカルモデルの明示的な loopback IP：", "接続先の正確なモデル ID（ワイルドカード不可）：", "認証情報：env:VARIABLE_NAME または API キー。キーは再表示しません：",
    ),
    "ko": (
        "모델 연결 목록. ⚙️ API 설정 가능, 미검증. 🔒 프로토콜·인증 미연결. 저장을 확인한 연결만 /model에 표시됩니다.", "사용자 지정",
        "입력은 연결 설정에만 쓰이며 에이전트에 전달되지 않습니다. /cancel로 취소하며 10분 후 만료됩니다.", "먼저 XDSH_PROVIDERS_ROOT와 관리 게이트웨이를 설정하세요.", "Bot과의 개인 채팅에서 설정하세요.", "설정이 혼잡합니다. 나중에 재시도하세요.", "이 프로토콜·인증은 연결되지 않았습니다. 자격 증명을 읽거나 로그인하지 않았습니다. /model add로 호환 게이트웨이를 설정하세요.",
        "설정 또는 승인이 만료되었습니다. 저장하지 않았습니다. /model add로 다시 시작하세요.", "취소했습니다. 새 연결은 저장되지 않았습니다.", "입력이 올바르지 않습니다. 필요한 형식을 확인하세요.", "저장하고 이 주소로 모델 요청과 해당 자격 증명을 보내도록 허용할까요? 키는 다시 표시하지 않지만 Telegram 원본 메시지는 남을 수 있습니다.", "연결을 저장했습니다. /model에서 선택하세요. 저장은 검증을 뜻하지 않습니다. 모델·도구·추론 지원은 제공자에 따라 다릅니다.", "연결을 저장하지 못했습니다. 중복 ID나 잘못된 설정을 확인하세요.",
        "제공자 ID (예: deepseek, local):", "프로토콜: anthropic / openai / gemini / azure:API_VERSION", "기본 URL: HTTPS 또는 로컬 모델의 명시적 loopback IP:", "상위 제공자의 정확한 모델 ID, 와일드카드 불가:", "자격 증명: env:VARIABLE_NAME 또는 API 키. 키는 다시 표시하지 않습니다:",
    ),
    "es": (
        "Catálogo de conexiones. ⚙️ API configurable, no verificada. 🔒 Protocolo/autenticación sin integrar. Solo conexiones confirmadas aparecen en /model.", "Personalizado",
        "Las entradas solo configuran la conexión; no van al agente. /cancel cancela; caduca en 10 minutos.", "Configura primero XDSH_PROVIDERS_ROOT y una pasarela administrada.", "Configura en un chat privado con el Bot.", "Configuración ocupada; inténtalo después.", "Protocolo/autenticación no integrados. No se leyeron credenciales ni se inició sesión. Usa /model add con una pasarela compatible.",
        "Configuración o aprobación caducada. Nada guardado; repite /model add.", "Cancelado; no se guardó una conexión nueva.", "Entrada inválida. Revisa el formato requerido.", "Confirma guardar y permitir solicitudes y credenciales a este destino. La clave no se muestra; el mensaje original de Telegram puede permanecer.", "Conexión guardada. Selecciona con /model. Guardar no es verificar: modelo, herramientas y razonamiento dependen del proveedor.", "Conexión no guardada. Revisa ID duplicados o configuración inválida.",
        "ID del proveedor (deepseek, local):", "Protocolo: anthropic / openai / gemini / azure:API_VERSION", "URL base: HTTPS o IP loopback explícita para modelos locales:", "ID exacto del modelo del proveedor, sin comodines:", "Credencial: env:VARIABLE_NAME o clave API. La clave no se muestra:",
    ),
    "fr": (
        "Catalogue des connexions. ⚙️ API configurable, non vérifiée. 🔒 Protocole/authentification non intégrés. Seules les connexions confirmées apparaissent dans /model.", "Personnalisé",
        "Les entrées configurent la connexion, pas une tâche agent. /cancel annule ; expiration dans 10 minutes.", "Configurez d’abord XDSH_PROVIDERS_ROOT et une passerelle gérée.", "Configurez dans une conversation privée avec le Bot.", "Configuration occupée ; réessayez plus tard.", "Protocole/authentification non intégrés. Aucun identifiant lu ni connexion lancée. Utilisez /model add avec une passerelle compatible.",
        "Configuration ou confirmation expirée. Rien enregistré ; relancez /model add.", "Annulé ; aucune nouvelle connexion enregistrée.", "Entrée invalide. Vérifiez le format requis.", "Confirmez l’enregistrement et l’envoi de requêtes et identifiants à cette adresse. La clé n’est pas réaffichée ; le message Telegram initial peut rester.", "Connexion enregistrée. Sélectionnez avec /model. Enregistré ne signifie pas vérifié : modèle, outils et raisonnement dépendent du fournisseur.", "Connexion non enregistrée. Vérifiez les ID en double ou la configuration.",
        "ID du fournisseur (deepseek, local) :", "Protocole : anthropic / openai / gemini / azure:API_VERSION", "URL de base : HTTPS ou IP loopback explicite pour les modèles locaux :", "ID exact du modèle distant, sans joker :", "Identifiant : env:VARIABLE_NAME ou clé API. La clé n’est pas réaffichée :",
    ),
    "de": (
        "Verbindungskatalog. ⚙️ API konfigurierbar, ungeprüft. 🔒 Protokoll/Anmeldung nicht angebunden. Nur bestätigte Verbindungen erscheinen unter /model.", "Benutzerdefiniert",
        "Eingaben dienen nur der Verbindung, nicht dem Agenten. /cancel bricht ab; Ablauf nach 10 Minuten.", "Zuerst XDSH_PROVIDERS_ROOT und ein verwaltetes Gateway konfigurieren.", "Im privaten Chat mit dem Bot konfigurieren.", "Einrichtung ausgelastet; später erneut versuchen.", "Protokoll/Anmeldung nicht angebunden. Keine Zugangsdaten gelesen, keine Anmeldung gestartet. /model add mit kompatiblem Gateway verwenden.",
        "Einrichtung oder Bestätigung abgelaufen. Nichts gespeichert; /model add erneut nutzen.", "Abgebrochen; keine neue Verbindung gespeichert.", "Ungültige Eingabe. Erforderliches Format prüfen.", "Speichern sowie Modellanfragen und zugehörige Zugangsdaten an dieses Ziel erlauben? Der Schlüssel wird nicht erneut angezeigt; die ursprüngliche Telegram-Nachricht kann bleiben.", "Verbindung gespeichert. Mit /model auswählen. Gespeichert bedeutet nicht geprüft: Modell-, Werkzeug- und Denkunterstützung hängen vom Anbieter ab.", "Verbindung nicht gespeichert. Doppelte IDs oder ungültige Konfiguration prüfen.",
        "Anbieter-ID (deepseek, local):", "Protokoll: anthropic / openai / gemini / azure:API_VERSION", "Basis-URL: HTTPS oder explizite Loopback-IP für lokale Modelle:", "Exakte Modell-ID des Anbieters, ohne Platzhalter:", "Zugangsdaten: env:VARIABLE_NAME oder API-Schlüssel. Schlüssel wird nicht erneut angezeigt:",
    ),
    "pt-BR": (
        "Catálogo de conexões. ⚙️ API configurável, não verificada. 🔒 Protocolo/autenticação não integrados. Só conexões confirmadas aparecem em /model.", "Personalizado",
        "Entradas configuram a conexão, não uma tarefa do agente. /cancel cancela; expira em 10 minutos.", "Configure primeiro XDSH_PROVIDERS_ROOT e um gateway gerenciado.", "Configure em conversa privada com o Bot.", "Configuração ocupada; tente depois.", "Protocolo/autenticação não integrados. Nenhuma credencial foi lida nem login iniciado. Use /model add com gateway compatível.",
        "Configuração ou confirmação expirada. Nada salvo; repita /model add.", "Cancelado; nenhuma conexão nova salva.", "Entrada inválida. Confira o formato necessário.", "Confirme salvar e permitir solicitações e credenciais para este destino. A chave não será exibida; a mensagem original do Telegram pode permanecer.", "Conexão salva. Selecione com /model. Salvar não é verificar: modelo, ferramentas e raciocínio dependem do provedor.", "Conexão não salva. Verifique IDs duplicados ou configuração inválida.",
        "ID do provedor (deepseek, local):", "Protocolo: anthropic / openai / gemini / azure:API_VERSION", "URL base: HTTPS ou IP loopback explícito para modelos locais:", "ID exato do modelo do provedor, sem curingas:", "Credencial: env:VARIABLE_NAME ou chave API. A chave não será exibida:",
    ),
    "ru": (
        "Каталог подключений. ⚙️ API настраивается, но не проверен. 🔒 Протокол/авторизация не подключены. В /model появляются только подтверждённые подключения.", "Свой вариант",
        "Ввод используется для настройки, не передаётся агенту. /cancel отменяет; срок 10 минут.", "Сначала настройте XDSH_PROVIDERS_ROOT и управляемый шлюз.", "Настраивайте в личном чате с ботом.", "Настройка занята; повторите позже.", "Протокол/авторизация не подключены. Учётные данные не читались, вход не запускался. Используйте /model add с совместимым шлюзом.",
        "Настройка или подтверждение истекли. Ничего не сохранено; повторите /model add.", "Отменено; новое подключение не сохранено.", "Неверный ввод. Проверьте требуемый формат.", "Подтвердите сохранение и отправку запросов и учётных данных этому адресу. Ключ не показывается повторно; исходное сообщение Telegram может остаться.", "Подключение сохранено. Выберите через /model. Сохранение не означает проверку: модели, инструменты и рассуждения зависят от поставщика.", "Подключение не сохранено. Проверьте повторяющиеся ID или неверные настройки.",
        "ID поставщика (deepseek, local):", "Протокол: anthropic / openai / gemini / azure:API_VERSION", "Базовый URL: HTTPS или явный loopback IP для локальных моделей:", "Точный ID модели поставщика, без шаблонов:", "Учётные данные: env:VARIABLE_NAME или API-ключ. Ключ не показывается повторно:",
    ),
    "ar": (
        "دليل الاتصالات. ⚙️ API قابل للإعداد، غير متحقق منه. 🔒 البروتوكول أو المصادقة غير موصولين. تظهر الاتصالات المؤكدة فقط في /model.", "مخصص",
        "المدخلات لإعداد الاتصال ولا تُرسل إلى الوكيل. /cancel للإلغاء؛ تنتهي خلال 10 دقائق.", "أعدّ XDSH_PROVIDERS_ROOT وبوابة مُدارة أولًا.", "استخدم المحادثة الخاصة مع البوت للإعداد.", "الإعداد مشغول؛ حاول لاحقًا.", "هذا البروتوكول أو المصادقة غير موصولين. لم تُقرأ بيانات اعتماد ولم يبدأ تسجيل دخول. استخدم /model add مع بوابة متوافقة.",
        "انتهى الإعداد أو التأكيد. لم يُحفظ شيء؛ أعد /model add.", "أُلغي؛ لم يُحفظ اتصال جديد.", "مدخل غير صالح. تحقّق من الصيغة المطلوبة.", "أكّد الحفظ والسماح بإرسال طلبات النموذج وبيانات الاعتماد إلى هذا العنوان. لا يُعرض المفتاح مجددًا؛ قد تبقى رسالة Telegram الأصلية.", "حُفظ الاتصال. اختره عبر /model. الحفظ ليس تحققًا؛ دعم النموذج والأدوات والاستدلال يعتمد على المزوّد.", "لم يُحفظ الاتصال. افحص تكرار المعرّفات أو إعدادًا غير صالح.",
        "معرّف المزوّد، مثل deepseek أو local:", "البروتوكول: anthropic / openai / gemini / azure:API_VERSION", "العنوان الأساسي: HTTPS أو عنوان loopback IP صريح للنماذج المحلية:", "معرّف النموذج الدقيق لدى المزوّد، دون أحرف بدل:", "بيانات الاعتماد: env:VARIABLE_NAME أو مفتاح API. لن يُعرض المفتاح مجددًا:",
    ),
    "hi": (
        "कनेक्शन सूची। ⚙️ API सेट किया जा सकता है, सत्यापित नहीं। 🔒 प्रोटोकॉल/प्रमाणीकरण नहीं जुड़ा है। केवल पुष्टि किए कनेक्शन /model में दिखते हैं।", "कस्टम",
        "इनपुट केवल कनेक्शन सेट करते हैं, एजेंट को नहीं जाते। /cancel रद्द करता है; 10 मिनट में समाप्त।", "पहले XDSH_PROVIDERS_ROOT और प्रबंधित गेटवे सेट करें।", "Bot की निजी चैट में सेट करें।", "सेटअप व्यस्त है; बाद में कोशिश करें।", "यह प्रोटोकॉल/प्रमाणीकरण नहीं जुड़ा है। क्रेडेंशियल नहीं पढ़े गए और लॉगिन शुरू नहीं हुआ। संगत गेटवे के साथ /model add चलाएँ।",
        "सेटअप या पुष्टि समाप्त हुई। कुछ नहीं सहेजा; /model add फिर चलाएँ।", "रद्द; नया कनेक्शन नहीं सहेजा।", "इनपुट अमान्य है। आवश्यक प्रारूप जाँचें।", "सहेजने और इस पते पर मॉडल अनुरोध व संबंधित क्रेडेंशियल भेजने की पुष्टि करें। कुंजी फिर नहीं दिखेगी; Telegram का मूल संदेश रह सकता है।", "कनेक्शन सहेजा। /model से चुनें। सहेजना सत्यापन नहीं है; मॉडल, टूल और तर्क समर्थन प्रदाता पर निर्भर हैं।", "कनेक्शन नहीं सहेजा। दोहराए ID या अमान्य सेटिंग जाँचें।",
        "प्रदाता ID, जैसे deepseek, local:", "प्रोटोकॉल: anthropic / openai / gemini / azure:API_VERSION", "मूल URL: HTTPS या स्थानीय मॉडल के लिए स्पष्ट loopback IP:", "प्रदाता का सटीक मॉडल ID, बिना वाइल्डकार्ड:", "क्रेडेंशियल: env:VARIABLE_NAME या API कुंजी। कुंजी फिर नहीं दिखाई जाएगी:",
    ),
    "id": (
        "Katalog koneksi. ⚙️ API dapat diatur, belum diverifikasi. 🔒 Protokol/autentikasi belum terhubung. Hanya koneksi terkonfirmasi muncul di /model.", "Kustom",
        "Masukan hanya mengatur koneksi, bukan tugas agen. /cancel membatalkan; kedaluwarsa dalam 10 menit.", "Atur XDSH_PROVIDERS_ROOT dan gateway terkelola dahulu.", "Atur lewat chat pribadi dengan Bot.", "Pengaturan sibuk; coba nanti.", "Protokol/autentikasi ini belum terhubung. Kredensial tidak dibaca dan login tidak dimulai. Gunakan /model add dengan gateway kompatibel.",
        "Pengaturan atau konfirmasi kedaluwarsa. Tidak disimpan; ulangi /model add.", "Dibatalkan; koneksi baru tidak disimpan.", "Masukan tidak valid. Periksa format yang diperlukan.", "Konfirmasikan penyimpanan dan izin mengirim permintaan model serta kredensial ke alamat ini. Kunci tidak ditampilkan ulang; pesan Telegram asli mungkin tetap ada.", "Koneksi disimpan. Pilih lewat /model. Disimpan bukan berarti terverifikasi; model, alat, dan penalaran bergantung pada penyedia.", "Koneksi tidak disimpan. Periksa ID ganda atau konfigurasi tidak valid.",
        "ID penyedia (deepseek, local):", "Protokol: anthropic / openai / gemini / azure:API_VERSION", "URL dasar: HTTPS atau IP loopback eksplisit untuk model lokal:", "ID model penyedia yang tepat, tanpa wildcard:", "Kredensial: env:VARIABLE_NAME atau kunci API. Kunci tidak ditampilkan ulang:",
    ),
}


def provider_ui(locale: str, key: str) -> str:
    try:
        index = _KEYS.index(key)
    except ValueError as error:
        raise KeyError(key) from error
    return _ROWS[canonical_language(locale)][index]


def validate_provider_ui() -> None:
    if set(_ROWS) != set(language_codes()) or any(
        len(row) != len(_KEYS) or any(not text for text in row) for row in _ROWS.values()
    ):
        raise ValueError("provider setup locale mismatch")
