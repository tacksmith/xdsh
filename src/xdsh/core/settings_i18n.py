"""Complete fixed-message catalog for reasoning/display settings.

Only host-owned templates are formatted. User content, provider identifiers and
callback values are not translated. No network translation is performed.
"""

from __future__ import annotations

from string import Formatter

from xdsh.core.i18n import canonical_language, language_codes

_EFFORT_ROWS = {
    "en": ("Off", "Minimal", "Low", "Medium", "High", "Extra high", "Maximum"),
    "zh-TW": ("關閉", "最低", "低", "中", "高", "超高", "最高"),
    "zh-CN": ("关闭", "最低", "低", "中", "高", "超高", "最高"),
    "ja": ("無効", "最小", "低", "中", "高", "非常に高", "最大"),
    "ko": ("끄기", "최소", "낮음", "중간", "높음", "매우 높음", "최대"),
    "es": ("Desactivado", "Mínimo", "Bajo", "Medio", "Alto", "Muy alto", "Máximo"),
    "fr": ("Désactivé", "Minimal", "Faible", "Moyen", "Élevé", "Très élevé", "Maximal"),
    "de": ("Aus", "Minimal", "Niedrig", "Mittel", "Hoch", "Sehr hoch", "Maximal"),
    "pt-BR": ("Desligado", "Mínimo", "Baixo", "Médio", "Alto", "Muito alto", "Máximo"),
    "ru": ("Выключено", "Минимально", "Низко", "Средне", "Высоко", "Очень высоко", "Максимально"),
    "ar": ("إيقاف", "أدنى", "منخفض", "متوسط", "مرتفع", "مرتفع جداً", "أقصى"),
    "hi": ("बंद", "न्यूनतम", "कम", "मध्यम", "उच्च", "बहुत उच्च", "अधिकतम"),
    "id": ("Mati", "Minimal", "Rendah", "Sedang", "Tinggi", "Sangat tinggi", "Maksimal"),
}


def effort_label(locale: str, effort: str) -> str:
    """Keep callback/API effort IDs stable while translating button labels."""
    index = ("off", "minimal", "low", "medium", "high", "xhigh", "max").index(effort)
    icon = ("⚪", "🟢", "🔵", "🟡", "🟠", "🔴", "🟣")[index]
    return f"{icon} {_EFFORT_ROWS[canonical_language(locale)][index]}"


_KEYS = (
    "thinking",
    "reasoning",
    "show",
    "hide",
    "display_note",
    "supported",
    "unsupported",
    "unknown",
    "busy",
    "saved",
    "usage",
    "budget",
    "generation",
)
_ROWS = {
    "en": (
        "Thinking output",
        "Reasoning effort",
        "Show",
        "Hide",
        "Display only; /reasoning controls effort. Shown content is bounded and credential-redacted.",
        "The route declares support.",
        "{model}: unsupported; keep {command} off or switch model.",
        "Support is undeclared; only actual events can confirm output.",
        "A task is running. Use /cancel before changing this setting.",
        "{session}: {setting} → {value}.",
        "Usage: {command}",
        "RC2 budget-based routes map xhigh/max to the high budget.",
        "Effective on the next task; session, files and logs are preserved.",
    ),
    "zh-TW": (
        "Thinking 輸出",
        "推理強度",
        "顯示",
        "隱藏",
        "僅控制顯示；/reasoning 控制推理強度。內容有長度上限並遮蔽常見憑據。",
        "目前路由已宣告支援。",
        "{model} 不支援此設定；請保持 {command} off 或切換模型。",
        "路由未宣告支援；以實際事件為準。",
        "目前任務仍在執行；請先 /cancel 再調整設定。",
        "{session}：{setting} → {value}。",
        "用法：{command}",
        "RC2 的預算型路由將 xhigh/max 映射到 high 預算。",
        "下一個任務生效；會話、檔案與日誌保留。",
    ),
    "zh-CN": (
        "Thinking 输出",
        "推理强度",
        "显示",
        "隐藏",
        "仅控制显示；/reasoning 控制推理强度。内容有长度上限并遮蔽常见凭据。",
        "当前路由已声明支持。",
        "{model} 不支持此设置；请保持 {command} off 或切换模型。",
        "路由未声明支持；以实际事件为准。",
        "当前任务仍在执行；请先 /cancel 再调整设置。",
        "{session}：{setting} → {value}。",
        "用法：{command}",
        "RC2 的预算型路由将 xhigh/max 映射到 high 预算。",
        "下一个任务生效；会话、文件与日志保留。",
    ),
    "ja": (
        "思考の表示",
        "推論強度",
        "表示",
        "非表示",
        "表示のみ変更します。強度は /reasoning で設定。表示内容には長さ制限と認証情報のマスキングがあります。",
        "対応が宣言されています。",
        "{model}: 非対応です。{command} off のままにするかモデルを変更してください。",
        "対応は未宣言です。実際のイベントで確認します。",
        "タスク実行中です。変更前に /cancel を実行してください。",
        "{session}: {setting} → {value}。",
        "使い方: {command}",
        "RC2 の予算型ルートでは xhigh/max は high 予算に対応します。",
        "次のタスクから有効です。会話・ファイル・ログは保持します。",
    ),
    "ko": (
        "사고 내용 표시",
        "추론 강도",
        "표시",
        "숨김",
        "표시만 변경합니다. 강도는 /reasoning으로 설정합니다. 내용 길이를 제한하고 인증 정보를 가립니다.",
        "지원이 선언되어 있습니다.",
        "{model}: 지원되지 않습니다. {command} off를 유지하거나 모델을 변경하세요.",
        "지원 여부가 선언되지 않았습니다. 실제 이벤트로 확인합니다.",
        "작업 실행 중입니다. 변경 전에 /cancel을 사용하세요.",
        "{session}: {setting} → {value}.",
        "사용법: {command}",
        "RC2 예산 기반 경로에서 xhigh/max는 high 예산으로 매핑됩니다.",
        "다음 작업부터 적용됩니다. 세션, 파일, 로그는 보존됩니다.",
    ),
    "es": (
        "Mostrar razonamiento",
        "Intensidad de razonamiento",
        "Mostrar",
        "Ocultar",
        "Solo cambia la visualización; /reasoning controla la intensidad. El contenido se limita y las credenciales se ocultan.",
        "La ruta declara compatibilidad.",
        "{model}: no compatible; mantén {command} off o cambia de modelo.",
        "Compatibilidad no declarada; comprueba los eventos reales.",
        "Hay una tarea activa. Usa /cancel antes de cambiar el ajuste.",
        "{session}: {setting} → {value}.",
        "Uso: {command}",
        "Las rutas RC2 basadas en presupuesto asignan xhigh/max al presupuesto high.",
        "Se aplica a la próxima tarea; se conservan sesión, archivos y registros.",
    ),
    "fr": (
        "Affichage du raisonnement",
        "Intensité du raisonnement",
        "Afficher",
        "Masquer",
        "Affichage uniquement ; /reasoning règle l’intensité. Le contenu est limité et les identifiants secrets masqués.",
        "La route déclare la prise en charge.",
        "{model} : non pris en charge ; garder {command} off ou changer de modèle.",
        "Prise en charge non déclarée ; seuls les événements réels confirment la sortie.",
        "Tâche en cours. Utilisez /cancel avant de modifier ce réglage.",
        "{session} : {setting} → {value}.",
        "Utilisation : {command}",
        "Les routes RC2 à budget associent xhigh/max au budget high.",
        "Effectif à la prochaine tâche ; session, fichiers et journaux conservés.",
    ),
    "de": (
        "Gedankenausgabe",
        "Schlussfolgerungsstärke",
        "Anzeigen",
        "Ausblenden",
        "Nur Anzeige; /reasoning steuert die Stärke. Inhalte werden begrenzt und Zugangsdaten maskiert.",
        "Die Route meldet Unterstützung.",
        "{model}: nicht unterstützt; {command} off beibehalten oder Modell wechseln.",
        "Unterstützung nicht deklariert; tatsächliche Ereignisse prüfen.",
        "Eine Aufgabe läuft. Vor Änderungen /cancel verwenden.",
        "{session}: {setting} → {value}.",
        "Verwendung: {command}",
        "Budgetbasierte RC2-Routen bilden xhigh/max auf das high-Budget ab.",
        "Gilt ab der nächsten Aufgabe; Sitzung, Dateien und Protokolle bleiben erhalten.",
    ),
    "pt-BR": (
        "Exibir raciocínio",
        "Intensidade de raciocínio",
        "Mostrar",
        "Ocultar",
        "Apenas exibição; /reasoning controla a intensidade. O conteúdo é limitado e as credenciais são ocultadas.",
        "A rota declara suporte.",
        "{model}: sem suporte; mantenha {command} off ou troque o modelo.",
        "Suporte não declarado; confirme pelos eventos reais.",
        "Há uma tarefa em execução. Use /cancel antes de alterar.",
        "{session}: {setting} → {value}.",
        "Uso: {command}",
        "Rotas RC2 baseadas em orçamento mapeiam xhigh/max para o orçamento high.",
        "Válido na próxima tarefa; sessão, arquivos e registros são preservados.",
    ),
    "ru": (
        "Вывод рассуждений",
        "Интенсивность рассуждений",
        "Показать",
        "Скрыть",
        "Только отображение; /reasoning задаёт интенсивность. Объём ограничен, учётные данные скрываются.",
        "Маршрут заявляет поддержку.",
        "{model}: не поддерживается; оставьте {command} off или смените модель.",
        "Поддержка не заявлена; проверяйте реальные события.",
        "Задача выполняется. Перед изменением используйте /cancel.",
        "{session}: {setting} → {value}.",
        "Использование: {command}",
        "В бюджетных маршрутах RC2 уровни xhigh/max соответствуют бюджету high.",
        "Действует со следующей задачи; сеанс, файлы и журналы сохраняются.",
    ),
    "ar": (
        "عرض التفكير",
        "شدة الاستدلال",
        "إظهار",
        "إخفاء",
        "للإظهار فقط؛ يتحكم /reasoning بالشدة. المحتوى محدود الطول وتُحجب بيانات الاعتماد.",
        "المسار يعلن الدعم.",
        "{model}: غير مدعوم؛ أبقِ {command} off أو غيّر النموذج.",
        "الدعم غير معلن؛ تحقّق من الأحداث الفعلية.",
        "هناك مهمة قيد التنفيذ. استخدم /cancel قبل التغيير.",
        "{session}: {setting} → {value}.",
        "الاستخدام: {command}",
        "تربط مسارات RC2 المعتمدة على الميزانية xhigh/max بميزانية high.",
        "يسري على المهمة التالية؛ تُحفظ الجلسة والملفات والسجلات.",
    ),
    "hi": (
        "विचार प्रदर्शन",
        "तर्क की तीव्रता",
        "दिखाएँ",
        "छिपाएँ",
        "केवल प्रदर्शन बदलता है; /reasoning तीव्रता नियंत्रित करता है। सामग्री सीमित है और क्रेडेंशियल छिपाए जाते हैं।",
        "रूट समर्थन घोषित करता है।",
        "{model}: समर्थित नहीं; {command} off रखें या मॉडल बदलें।",
        "समर्थन घोषित नहीं है; वास्तविक घटनाओं से पुष्टि करें।",
        "कार्य चल रहा है। बदलाव से पहले /cancel करें।",
        "{session}: {setting} → {value}।",
        "उपयोग: {command}",
        "RC2 बजट रूट में xhigh/max को high बजट मिलता है।",
        "अगले कार्य से लागू; सत्र, फ़ाइलें और लॉग सुरक्षित रहते हैं।",
    ),
    "id": (
        "Tampilan pemikiran",
        "Tingkat penalaran",
        "Tampilkan",
        "Sembunyikan",
        "Hanya tampilan; /reasoning mengatur tingkat penalaran. Isi dibatasi dan kredensial disamarkan.",
        "Rute menyatakan dukungan.",
        "{model}: tidak didukung; pertahankan {command} off atau ganti model.",
        "Dukungan belum dinyatakan; periksa peristiwa nyata.",
        "Tugas sedang berjalan. Gunakan /cancel sebelum mengubah pengaturan.",
        "{session}: {setting} → {value}.",
        "Penggunaan: {command}",
        "Rute RC2 berbasis anggaran memetakan xhigh/max ke anggaran high.",
        "Berlaku pada tugas berikutnya; sesi, berkas, dan log tetap tersimpan.",
    ),
}


_NAV_KEYS = (
    "session",
    "sessions",
    "models",
    "providers",
    "manage",
    "new_session",
    "clear_sessions",
    "fallback",
    "add_model",
)
_NAV_ROWS = {
    "en": (
        "Session",
        "Sessions",
        "Models",
        "Providers",
        "Manage",
        "New session",
        "Clear all sessions",
        "Fallback",
        "Add model connection",
    ),
    "zh-TW": (
        "會話",
        "會話列表",
        "模型",
        "提供商",
        "管理",
        "新會話",
        "清空全部會話",
        "備用模型",
        "新增模型接入",
    ),
    "zh-CN": (
        "会话",
        "会话列表",
        "模型",
        "提供商",
        "管理",
        "新会话",
        "清空全部会话",
        "备用模型",
        "新增模型接入",
    ),
    "ja": (
        "会話",
        "会話一覧",
        "モデル",
        "プロバイダー",
        "管理",
        "新しい会話",
        "すべての会話をクリア",
        "代替モデル",
        "モデル接続を追加",
    ),
    "ko": (
        "세션",
        "세션 목록",
        "모델",
        "제공자",
        "관리",
        "새 세션",
        "모든 세션 비우기",
        "대체 모델",
        "모델 연결 추가",
    ),
    "es": (
        "Sesión",
        "Sesiones",
        "Modelos",
        "Proveedores",
        "Gestionar",
        "Nueva sesión",
        "Vaciar todas las sesiones",
        "Modelo alternativo",
        "Añadir conexión de modelo",
    ),
    "fr": (
        "Session",
        "Sessions",
        "Modèles",
        "Fournisseurs",
        "Gérer",
        "Nouvelle session",
        "Vider toutes les sessions",
        "Modèle de secours",
        "Ajouter une connexion de modèle",
    ),
    "de": (
        "Sitzung",
        "Sitzungen",
        "Modelle",
        "Anbieter",
        "Verwalten",
        "Neue Sitzung",
        "Alle Sitzungen leeren",
        "Ersatzmodell",
        "Modellverbindung hinzufügen",
    ),
    "pt-BR": (
        "Sessão",
        "Sessões",
        "Modelos",
        "Provedores",
        "Gerenciar",
        "Nova sessão",
        "Limpar todas as sessões",
        "Modelo alternativo",
        "Adicionar conexão de modelo",
    ),
    "ru": (
        "Сеанс",
        "Сеансы",
        "Модели",
        "Провайдеры",
        "Управление",
        "Новый сеанс",
        "Очистить все сеансы",
        "Резервная модель",
        "Добавить подключение модели",
    ),
    "ar": (
        "جلسة",
        "الجلسات",
        "النماذج",
        "المزودون",
        "إدارة",
        "جلسة جديدة",
        "مسح جميع الجلسات",
        "النموذج البديل",
        "إضافة اتصال نموذج",
    ),
    "hi": (
        "सत्र",
        "सत्र सूची",
        "मॉडल",
        "प्रदाता",
        "प्रबंधन",
        "नया सत्र",
        "सभी सत्र साफ़ करें",
        "वैकल्पिक मॉडल",
        "मॉडल कनेक्शन जोड़ें",
    ),
    "id": (
        "Sesi",
        "Daftar sesi",
        "Model",
        "Penyedia",
        "Kelola",
        "Sesi baru",
        "Kosongkan semua sesi",
        "Model cadangan",
        "Tambah koneksi model",
    ),
}


_STATUS_KEYS = (
    "status",
    "runtime",
    "capabilities",
    "protection",
    "background",
    "reliability",
    "versions",
    "rate",
    "name",
    "diagnostics",
)
_STATUS_ROWS = {
    "en": (
        "Status",
        "Runtime",
        "Capabilities",
        "Protection",
        "Background jobs",
        "Reliability",
        "Versions",
        "Rate limit",
        "Name",
        "Diagnostics",
    ),
    "zh-TW": (
        "狀態",
        "執行環境",
        "能力",
        "防護",
        "背景工作",
        "可靠性",
        "版本",
        "限流",
        "名稱",
        "診斷",
    ),
    "zh-CN": (
        "状态",
        "执行环境",
        "能力",
        "防护",
        "后台任务",
        "可靠性",
        "版本",
        "限流",
        "名称",
        "诊断",
    ),
    "ja": (
        "状態",
        "実行環境",
        "機能",
        "保護",
        "バックグラウンドタスク",
        "信頼性",
        "バージョン",
        "レート制限",
        "名前",
        "診断",
    ),
    "ko": (
        "상태",
        "실행 환경",
        "기능",
        "보호",
        "백그라운드 작업",
        "신뢰성",
        "버전",
        "요청 제한",
        "이름",
        "진단",
    ),
    "es": (
        "Estado",
        "Entorno de ejecución",
        "Capacidades",
        "Protección",
        "Tareas en segundo plano",
        "Fiabilidad",
        "Versiones",
        "Límite de solicitudes",
        "Nombre",
        "Diagnóstico",
    ),
    "fr": (
        "État",
        "Environnement",
        "Capacités",
        "Protection",
        "Tâches en arrière-plan",
        "Fiabilité",
        "Versions",
        "Limite de requêtes",
        "Nom",
        "Diagnostic",
    ),
    "de": (
        "Status",
        "Laufzeitumgebung",
        "Fähigkeiten",
        "Schutz",
        "Hintergrundaufgaben",
        "Zuverlässigkeit",
        "Versionen",
        "Anfragelimit",
        "Name",
        "Diagnose",
    ),
    "pt-BR": (
        "Estado",
        "Ambiente de execução",
        "Capacidades",
        "Proteção",
        "Tarefas em segundo plano",
        "Confiabilidade",
        "Versões",
        "Limite de solicitações",
        "Nome",
        "Diagnóstico",
    ),
    "ru": (
        "Состояние",
        "Среда выполнения",
        "Возможности",
        "Защита",
        "Фоновые задачи",
        "Надёжность",
        "Версии",
        "Ограничение запросов",
        "Имя",
        "Диагностика",
    ),
    "ar": (
        "الحالة",
        "بيئة التشغيل",
        "القدرات",
        "الحماية",
        "مهام الخلفية",
        "الموثوقية",
        "الإصدارات",
        "حد الطلبات",
        "الاسم",
        "التشخيص",
    ),
    "hi": (
        "स्थिति",
        "रनटाइम",
        "क्षमताएँ",
        "सुरक्षा",
        "पृष्ठभूमि कार्य",
        "विश्वसनीयता",
        "संस्करण",
        "अनुरोध सीमा",
        "नाम",
        "निदान",
    ),
    "id": (
        "Status",
        "Lingkungan eksekusi",
        "Kemampuan",
        "Perlindungan",
        "Tugas latar belakang",
        "Keandalan",
        "Versi",
        "Batas permintaan",
        "Nama",
        "Diagnostik",
    ),
}


def static_ui_label(locale: str, text: str) -> str:
    """Migrate exact legacy host labels, never substrings or user prose."""
    aliases = {
        "📍 XDSH status": ("📍 XDSH ", "status"),
        "🩺 XDSH diagnostics": ("🩺 XDSH ", "diagnostics"),
        "🏷️ Versions": ("🏷️ ", "versions"),
        "🤖 Runtime": ("🤖 ", "runtime"),
        "🧠 Capabilities": ("🧠 ", "capabilities"),
        "🛡️ Protection": ("🛡️ ", "protection"),
        "🛡️ Traffic protection": ("🛡️ ", "protection"),
        "🧩 Background jobs": ("🧩 ", "background"),
        "🧩 Background work": ("🧩 ", "background"),
        "🧩 Job board": ("🧩 ", "background"),
        "📊 Reliability": ("📊 ", "reliability"),
        "💬 Session": ("💬 ", "session"),
        "🤖 Model": ("🤖 ", "models"),
        "Name": ("", "name"),
        "Reasoning": ("", "reasoning"),
        "Rate limit": ("", "rate"),
        "Manage": ("", "manage"),
        "show": ("", "show"),
        "hide": ("", "hide"),
    }
    matched = aliases.get(text)
    if matched is None or canonical_language(locale) == "en":
        return text
    prefix, key = matched
    return prefix + settings_text(locale, key)


def settings_text(locale: str, key: str, **values: object) -> str:
    """Get a localized template; unknown keys fail instead of mixing locales."""
    if key in _NAV_KEYS:
        return _NAV_ROWS[canonical_language(locale)][_NAV_KEYS.index(key)]
    if key in _STATUS_KEYS:
        return _STATUS_ROWS[canonical_language(locale)][_STATUS_KEYS.index(key)]
    try:
        index = _KEYS.index(key)
    except ValueError as error:
        raise KeyError(key) from error
    return _ROWS[canonical_language(locale)][index].format(**values)


def validate_settings_catalog() -> None:
    """Check locale coverage and identical interpolation fields."""
    if set(_ROWS) != set(language_codes()):
        raise ValueError("settings locale coverage mismatch")
    if set(_STATUS_ROWS) != set(language_codes()) or any(
        len(row) != len(_STATUS_KEYS) or any(not value for value in row)
        for row in _STATUS_ROWS.values()
    ):
        raise ValueError("settings status coverage mismatch")
    if set(_NAV_ROWS) != set(language_codes()) or any(
        len(row) != len(_NAV_KEYS) or any(not value for value in row) for row in _NAV_ROWS.values()
    ):
        raise ValueError("settings navigation coverage mismatch")
    if set(_EFFORT_ROWS) != set(language_codes()) or any(
        len(row) != 7 or any(not label for label in row) for row in _EFFORT_ROWS.values()
    ):
        raise ValueError("effort label coverage mismatch")
    formatter = Formatter()
    for row in _ROWS.values():
        if len(row) != len(_KEYS):
            raise ValueError("settings key coverage mismatch")
        for value, english in zip(row, _ROWS["en"], strict=True):
            fields = {key for _, key, _, _ in formatter.parse(value) if key is not None}
            expected = {key for _, key, _, _ in formatter.parse(english) if key is not None}
            if not value.strip() or fields != expected:
                raise ValueError("settings placeholder mismatch")
