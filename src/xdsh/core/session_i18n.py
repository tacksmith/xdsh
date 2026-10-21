"""Localized session clearing contract; never implies workspace deletion."""

from __future__ import annotations

from string import Formatter

from xdsh.core.i18n import canonical_language, language_codes

_KEYS = ("clear_prompt", "busy", "expired", "cleared")
_ROWS = {
    "en": (
        "Clear all {count} sessions in this chat? A fresh session keeps model settings. Workspace, memory and logs remain. Confirm within 2 minutes.",
        "Finish active and queued tasks before clearing sessions.",
        "State changed or confirmation expired. Use /session clear-all again.",
        "Session list cleared; fresh session {id} created. Files, memory and logs retained.",
    ),
    "zh-TW": (
        "清空本聊天全部 {count} 個會話？建立空白會話並保留模型設定。工作區、記憶與日誌不刪除。確認兩分鐘內有效。",
        "請先結束目前任務並處理排隊任務，再清空會話。",
        "狀態已變更或確認過期；請重新使用 /session clear-all。",
        "已清空會話列表，建立空白會話 {id}。檔案、記憶與日誌保留。",
    ),
    "zh-CN": (
        "清空本聊天全部 {count} 个会话？创建空白会话并保留模型设置。工作区、记忆与日志不删除。确认两分钟内有效。",
        "请先结束当前任务并处理排队任务，再清空会话。",
        "状态已变更或确认过期；请重新使用 /session clear-all。",
        "已清空会话列表，创建空白会话 {id}。文件、记忆与日志保留。",
    ),
    "ja": (
        "このチャットの全 {count} 会話をクリアしますか？設定を保持した新しい会話を作成します。作業ファイル・記憶・ログは削除しません。2分以内に確認してください。",
        "会話をクリアする前に、実行中と待機中のタスクを処理してください。",
        "状態が変わったか確認期限が切れました。/session clear-all を再実行してください。",
        "会話一覧をクリアし、新しい会話 {id} を作成しました。ファイル・記憶・ログは保持します。",
    ),
    "ko": (
        "이 채팅의 세션 {count}개를 모두 비울까요? 모델 설정을 유지한 새 세션을 만듭니다. 작업 파일, 기억, 로그는 삭제하지 않습니다. 2분 안에 확인하세요.",
        "세션을 비우기 전에 실행 중이거나 대기 중인 작업을 처리하세요.",
        "상태가 변경되었거나 확인이 만료되었습니다. /session clear-all을 다시 사용하세요.",
        "세션 목록을 비우고 새 세션 {id}를 만들었습니다. 파일, 기억, 로그는 유지됩니다.",
    ),
    "es": (
        "¿Vaciar las {count} sesiones de este chat? Se crea una nueva conservando la configuración. Archivos, memoria y registros permanecen. Confirma en 2 minutos.",
        "Resuelve las tareas activas y en cola antes de vaciar sesiones.",
        "Estado cambiado o confirmación caducada. Repite /session clear-all.",
        "Lista vaciada; nueva sesión {id}. Archivos, memoria y registros conservados.",
    ),
    "fr": (
        "Vider les {count} sessions de ce chat ? Une nouvelle conserve les réglages. Fichiers, mémoire et journaux restent. Confirmez sous 2 minutes.",
        "Terminez les tâches actives et en attente avant de vider les sessions.",
        "État changé ou confirmation expirée. Relancez /session clear-all.",
        "Liste vidée ; nouvelle session {id}. Fichiers, mémoire et journaux conservés.",
    ),
    "de": (
        "Alle {count} Sitzungen dieses Chats leeren? Eine neue behält die Einstellungen. Dateien, Gedächtnis und Protokolle bleiben erhalten. Innerhalb von 2 Minuten bestätigen.",
        "Aktive und wartende Aufgaben vor dem Leeren erledigen.",
        "Zustand geändert oder Bestätigung abgelaufen. /session clear-all erneut verwenden.",
        "Sitzungsliste geleert; neue Sitzung {id}. Dateien, Gedächtnis und Protokolle bleiben erhalten.",
    ),
    "pt-BR": (
        "Limpar as {count} sessões deste chat? Uma nova mantém as configurações. Arquivos, memória e registros permanecem. Confirme em 2 minutos.",
        "Resolva tarefas ativas e na fila antes de limpar sessões.",
        "Estado alterado ou confirmação expirada. Repita /session clear-all.",
        "Lista limpa; nova sessão {id}. Arquivos, memória e registros preservados.",
    ),
    "ru": (
        "Очистить все {count} сеансов этого чата? Новый сеанс сохранит настройки. Файлы, память и журналы останутся. Подтвердите в течение 2 минут.",
        "Перед очисткой завершите активные задачи и разберите очередь.",
        "Состояние изменилось или подтверждение истекло. Повторите /session clear-all.",
        "Список очищен; создан сеанс {id}. Файлы, память и журналы сохранены.",
    ),
    "ar": (
        "مسح جميع جلسات هذه المحادثة وعددها {count}؟ تُنشأ جلسة جديدة تحتفظ بالإعدادات. تبقى الملفات والذاكرة والسجلات. أكّد خلال دقيقتين.",
        "عالج المهام النشطة والمنتظرة قبل مسح الجلسات.",
        "تغيّرت الحالة أو انتهت صلاحية التأكيد. أعد /session clear-all.",
        "مُسحت القائمة وأُنشئت الجلسة {id}. الملفات والذاكرة والسجلات محفوظة.",
    ),
    "hi": (
        "इस चैट के सभी {count} सत्र साफ़ करें? नया सत्र सेटिंग रखेगा। फ़ाइलें, स्मृति और लॉग नहीं हटेंगे। 2 मिनट में पुष्टि करें।",
        "सत्र साफ़ करने से पहले सक्रिय और कतार वाले कार्य निपटाएँ।",
        "स्थिति बदली या पुष्टि समाप्त हुई। /session clear-all फिर चलाएँ।",
        "सूची साफ़; नया सत्र {id} बना। फ़ाइलें, स्मृति और लॉग सुरक्षित हैं।",
    ),
    "id": (
        "Kosongkan semua {count} sesi chat ini? Sesi baru mempertahankan pengaturan. Berkas, memori, dan log tetap ada. Konfirmasi dalam 2 menit.",
        "Selesaikan tugas aktif dan antrean sebelum mengosongkan sesi.",
        "Status berubah atau konfirmasi kedaluwarsa. Ulangi /session clear-all.",
        "Daftar dikosongkan; sesi baru {id}. Berkas, memori, dan log tetap tersimpan.",
    ),
}


_SELECTION_KEYS = ("created", "switched")
_ERROR_KEYS = ("state", "limit", "last", "missing", "duplicate", "name")
_ERROR_ROWS = {
    "en": ("Session state unavailable. Check /doctor before retrying.", "This chat allows {limit} sessions. Use /session to manage them.", "The last session cannot be deleted.", "Session not found. Choose one with /session.", "Session name already exists. Choose another name.", "Use 1–48 visible characters for the session name."),
    "zh-TW": ("會話狀態不可用；重試前請檢查 /doctor。", "本聊天最多保留 {limit} 個會話，請用 /session 管理。", "不能刪除最後一個會話。", "找不到會話，請用 /session 選擇。", "會話名稱已存在，請使用其他名稱。", "會話名稱須為 1–48 個可見字元。"),
    "zh-CN": ("会话状态不可用；重试前请检查 /doctor。", "本聊天最多保留 {limit} 个会话，请用 /session 管理。", "不能删除最后一个会话。", "找不到会话，请用 /session 选择。", "会话名称已存在，请使用其他名称。", "会话名称须为 1–48 个可见字符。"),
    "ja": ("会話の状態を利用できません。再試行前に /doctor を確認してください。", "会話の上限は {limit} 件です。/session で管理してください。", "最後の会話は削除できません。", "会話が見つかりません。/session で選択してください。", "同じ会話名が存在します。別の名前を選んでください。", "会話名は表示可能な1〜48文字にしてください。"),
    "ko": ("세션 상태를 사용할 수 없습니다. 재시도 전에 /doctor를 확인하세요.", "이 채팅은 세션 {limit}개까지 허용합니다. /session으로 관리하세요.", "마지막 세션은 삭제할 수 없습니다.", "세션을 찾지 못했습니다. /session에서 선택하세요.", "이미 있는 세션 이름입니다. 다른 이름을 선택하세요.", "세션 이름은 표시 가능한 1–48자로 입력하세요."),
    "es": ("Estado de sesión no disponible. Revisa /doctor antes de reintentar.", "Este chat admite {limit} sesiones. Adminístralas con /session.", "No se puede borrar la última sesión.", "Sesión no encontrada. Elige con /session.", "El nombre ya existe. Elige otro.", "Usa de 1 a 48 caracteres visibles para el nombre."),
    "fr": ("État de session indisponible. Vérifiez /doctor avant de réessayer.", "Ce chat accepte {limit} sessions. Gérez-les avec /session.", "La dernière session ne peut pas être supprimée.", "Session introuvable. Choisissez avec /session.", "Ce nom existe déjà. Choisissez-en un autre.", "Utilisez 1 à 48 caractères visibles pour le nom."),
    "de": ("Sitzungszustand nicht verfügbar. Vor erneutem Versuch /doctor prüfen.", "Dieser Chat erlaubt {limit} Sitzungen. Mit /session verwalten.", "Die letzte Sitzung kann nicht gelöscht werden.", "Sitzung nicht gefunden. Mit /session auswählen.", "Sitzungsname bereits vorhanden. Anderen Namen wählen.", "Für den Namen 1–48 sichtbare Zeichen verwenden."),
    "pt-BR": ("Estado da sessão indisponível. Verifique /doctor antes de tentar novamente.", "Este chat permite {limit} sessões. Gerencie com /session.", "Não é possível excluir a última sessão.", "Sessão não encontrada. Escolha com /session.", "O nome já existe. Escolha outro.", "Use de 1 a 48 caracteres visíveis no nome."),
    "ru": ("Состояние сеанса недоступно. Перед повтором проверьте /doctor.", "В чате допускается {limit} сеансов. Управление: /session.", "Последний сеанс нельзя удалить.", "Сеанс не найден. Выберите через /session.", "Имя уже существует. Выберите другое.", "Имя должно содержать 1–48 видимых символов."),
    "ar": ("حالة الجلسة غير متاحة. افحص /doctor قبل إعادة المحاولة.", "تسمح هذه المحادثة بـ {limit} جلسة. أدرها عبر /session.", "لا يمكن حذف الجلسة الأخيرة.", "الجلسة غير موجودة. اختر عبر /session.", "اسم الجلسة موجود بالفعل. اختر اسمًا آخر.", "استخدم من 1 إلى 48 حرفًا مرئيًا لاسم الجلسة."),
    "hi": ("सत्र की स्थिति उपलब्ध नहीं है। फिर कोशिश करने से पहले /doctor जाँचें।", "इस चैट में {limit} सत्र रख सकते हैं। /session से प्रबंधित करें।", "अंतिम सत्र नहीं हटाया जा सकता।", "सत्र नहीं मिला। /session से चुनें।", "यह नाम पहले से है। दूसरा नाम चुनें।", "सत्र नाम में 1–48 दिखाई देने वाले अक्षर रखें।"),
    "id": ("Status sesi tidak tersedia. Periksa /doctor sebelum mencoba lagi.", "Chat ini mengizinkan {limit} sesi. Kelola dengan /session.", "Sesi terakhir tidak dapat dihapus.", "Sesi tidak ditemukan. Pilih melalui /session.", "Nama sesi sudah ada. Pilih nama lain.", "Gunakan 1–48 karakter terlihat untuk nama sesi."),
}
_SELECTION_ROWS = {
    "en": ("Created and switched to session {name} ({id}).", "Switched to session {name} ({id})."),
    "zh-TW": ("已建立並切換到會話 {name} ({id})。", "已切換到會話 {name} ({id})。"),
    "zh-CN": ("已创建并切换到会话 {name} ({id})。", "已切换到会话 {name} ({id})。"),
    "ja": ("会話 {name} ({id}) を作成して切り替えました。", "会話 {name} ({id}) に切り替えました。"),
    "ko": ("세션 {name} ({id})을 만들고 전환했습니다.", "세션 {name} ({id})으로 전환했습니다."),
    "es": ("Sesión {name} ({id}) creada y seleccionada.", "Sesión {name} ({id}) seleccionada."),
    "fr": ("Session {name} ({id}) créée et sélectionnée.", "Session {name} ({id}) sélectionnée."),
    "de": ("Sitzung {name} ({id}) erstellt und ausgewählt.", "Zu Sitzung {name} ({id}) gewechselt."),
    "pt-BR": ("Sessão {name} ({id}) criada e selecionada.", "Sessão {name} ({id}) selecionada."),
    "ru": ("Создан и выбран сеанс {name} ({id}).", "Выбран сеанс {name} ({id})."),
    "ar": ("أُنشئت الجلسة {name} ({id}) وتم التبديل إليها.", "تم التبديل إلى الجلسة {name} ({id})."),
    "hi": ("सत्र {name} ({id}) बनाया और चुना गया।", "सत्र {name} ({id}) चुना गया।"),
    "id": ("Sesi {name} ({id}) dibuat dan dipilih.", "Beralih ke sesi {name} ({id})."),
}


def session_text(locale: str, key: str, **values: object) -> str:
    if key.startswith("error.") and key[6:] in _ERROR_KEYS:
        return _ERROR_ROWS[canonical_language(locale)][_ERROR_KEYS.index(key[6:])].format(**values)
    if key in _SELECTION_KEYS:
        return _SELECTION_ROWS[canonical_language(locale)][_SELECTION_KEYS.index(key)].format(**values)
    try:
        index = _KEYS.index(key)
    except ValueError as error:
        raise KeyError(key) from error
    return _ROWS[canonical_language(locale)][index].format(**values)


def validate_session_catalog() -> None:
    if set(_ERROR_ROWS) != set(language_codes()):
        raise ValueError("session error locale coverage mismatch")
    for error_row in _ERROR_ROWS.values():
        if len(error_row) != len(_ERROR_KEYS):
            raise ValueError("session error key mismatch")
        for key, message in zip(_ERROR_KEYS, error_row, strict=True):
            fields = {field for _, field, _, _ in Formatter().parse(message) if field is not None}
            if not message or fields != ({"limit"} if key == "limit" else set()):
                raise ValueError("session error placeholder mismatch")
    if set(_SELECTION_ROWS) != set(language_codes()):
        raise ValueError("session selection locale coverage mismatch")
    for selection_row in _SELECTION_ROWS.values():
        if len(selection_row) != len(_SELECTION_KEYS):
            raise ValueError("session selection key mismatch")
        for message in selection_row:
            fields = {key for _, key, _, _ in Formatter().parse(message) if key is not None}
            if fields != {"name", "id"}:
                raise ValueError("session selection placeholder mismatch")
    if set(_ROWS) != set(language_codes()):
        raise ValueError("session locale coverage mismatch")
    for row in _ROWS.values():
        if len(row) != len(_KEYS):
            raise ValueError("session message key mismatch")
        for message, english in zip(row, _ROWS["en"], strict=True):
            fields = {key for _, key, _, _ in Formatter().parse(message) if key is not None}
            expected = {key for _, key, _, _ in Formatter().parse(english) if key is not None}
            if not message or fields != expected:
                raise ValueError("session message placeholder mismatch")
