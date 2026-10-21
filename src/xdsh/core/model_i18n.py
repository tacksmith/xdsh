"""Model selection messages; exact model/provider IDs are never translated."""

from __future__ import annotations

from string import Formatter

from xdsh.core.i18n import canonical_language, language_codes

_KEYS = ("unavailable", "stale", "unknown", "probe_failed", "no_route", "changed", "reset")
_ROWS = {
    "en": (
        "Model catalog unavailable. Current model: {model}. Retry /model later.",
        "Selection expired. Open /model again.",
        "Unknown provider or model: {model}. Use /model.",
        "{model}: execution probe failed ({detail}); selection unchanged. Choose a healthy route in /model.",
        "{model}: no explicit route. The catch-all would substitute another model; selection unchanged. Configure the exact route, not just a menu alias.",
        "{session}: model switched to {model} ({provider}). Session and logs remain; RC2 uses a new DSH generation.",
        "Adjusted for this model: {setting} → {value}.",
    ),
    "zh-TW": (
        "模型目錄暫時不可用。目前模型: {model}。請稍後重試 /model。",
        "選擇已過期；請重新開啟 /model。",
        "未知提供商或模型: {model}。請使用 /model。",
        "{model} 的最小執行探測失敗 ({detail})；模型未切換。請在 /model 選擇健康路由。",
        "{model} 尚未配置明確路由；通配規則會改用另一模型，本次未切換。請配置精確路由，不能只新增選單別名。",
        "{session} 的模型已切換為 {model} ({provider})。會話與日誌保留；RC2 會使用新的 DSH 執行代。",
        "因應此模型：{setting} 已重設為 {value}。",
    ),
    "zh-CN": (
        "模型目录暂时不可用。当前模型: {model}。请稍后重试 /model。",
        "选择已过期；请重新打开 /model。",
        "未知提供商或模型: {model}。请使用 /model。",
        "{model} 的最小执行探测失败 ({detail})；模型未切换。请在 /model 选择健康路由。",
        "{model} 尚未配置明确路由；通配规则会改用另一模型，本次未切换。请配置精确路由，不能只新增菜单别名。",
        "{session} 的模型已切换为 {model} ({provider})。会话与日志保留；RC2 会使用新的 DSH 执行代。",
        "适配此模型：{setting} 已重置为 {value}。",
    ),
    "ja": (
        "モデル一覧を取得できません。現在のモデル: {model}。後で /model を再試行してください。",
        "選択の期限が切れました。/model を開き直してください。",
        "不明なプロバイダーまたはモデル: {model}。/model を使用してください。",
        "{model}: 実行確認失敗 ({detail})。選択は変更していません。/model で正常なルートを選んでください。",
        "{model}: 明示的なルートがありません。包括ルールでは別モデルになるため変更しません。別名だけでなく正確なルートを設定してください。",
        "{session}: モデルを {model} ({provider}) に変更しました。会話とログを保持し、RC2 は新しい DSH 実行世代を使います。",
        "モデルに合わせて変更: {setting} → {value}。",
    ),
    "ko": (
        "모델 목록을 사용할 수 없습니다. 현재 모델: {model}. 나중에 /model을 다시 시도하세요.",
        "선택이 만료되었습니다. /model을 다시 여세요.",
        "알 수 없는 제공자 또는 모델: {model}. /model을 사용하세요.",
        "{model}: 실행 확인 실패 ({detail}). 선택은 유지됩니다. /model에서 정상 경로를 선택하세요.",
        "{model}: 명시적 경로가 없습니다. 기본 규칙이 다른 모델로 바꾸므로 선택을 유지합니다. 별칭만 추가하지 말고 정확한 경로를 설정하세요.",
        "{session}: 모델을 {model} ({provider})로 변경했습니다. 세션과 로그는 유지되며 RC2는 새 DSH 실행 세대를 사용합니다.",
        "모델에 맞게 변경: {setting} → {value}.",
    ),
    "es": (
        "Catálogo no disponible. Modelo actual: {model}. Reintenta /model más tarde.",
        "Selección caducada. Abre /model de nuevo.",
        "Proveedor o modelo desconocido: {model}. Usa /model.",
        "{model}: prueba de ejecución fallida ({detail}); selección sin cambios. Elige una ruta sana en /model.",
        "{model}: falta una ruta explícita. La regla general usaría otro modelo; sin cambios. Configura la ruta exacta, no solo un alias.",
        "{session}: modelo cambiado a {model} ({provider}). Se conservan sesión y registros; RC2 usa una nueva generación DSH.",
        "Ajustado para este modelo: {setting} → {value}.",
    ),
    "fr": (
        "Catalogue indisponible. Modèle actuel : {model}. Réessayez /model plus tard.",
        "Sélection expirée. Rouvrez /model.",
        "Fournisseur ou modèle inconnu : {model}. Utilisez /model.",
        "{model} : test d’exécution échoué ({detail}) ; sélection inchangée. Choisissez une route saine dans /model.",
        "{model} : aucune route explicite. La règle générale utiliserait un autre modèle ; aucun changement. Configurez la route exacte, pas seulement un alias.",
        "{session} : modèle changé en {model} ({provider}). Session et journaux conservés ; RC2 utilise une nouvelle génération DSH.",
        "Ajustement pour ce modèle : {setting} → {value}.",
    ),
    "de": (
        "Modellkatalog nicht verfügbar. Aktuelles Modell: {model}. /model später erneut versuchen.",
        "Auswahl abgelaufen. /model erneut öffnen.",
        "Unbekannter Anbieter oder Modell: {model}. /model verwenden.",
        "{model}: Ausführungstest fehlgeschlagen ({detail}); Auswahl unverändert. Gesunde Route in /model wählen.",
        "{model}: keine explizite Route. Die Standardregel würde ein anderes Modell verwenden; keine Änderung. Exakte Route konfigurieren, nicht nur einen Alias.",
        "{session}: Modell auf {model} ({provider}) geändert. Sitzung und Protokolle bleiben erhalten; RC2 nutzt eine neue DSH-Generation.",
        "Für dieses Modell angepasst: {setting} → {value}.",
    ),
    "pt-BR": (
        "Catálogo indisponível. Modelo atual: {model}. Tente /model mais tarde.",
        "Seleção expirada. Abra /model novamente.",
        "Provedor ou modelo desconhecido: {model}. Use /model.",
        "{model}: teste de execução falhou ({detail}); seleção inalterada. Escolha uma rota saudável em /model.",
        "{model}: sem rota explícita. A regra geral usaria outro modelo; nada mudou. Configure a rota exata, não apenas um apelido.",
        "{session}: modelo alterado para {model} ({provider}). Sessão e registros preservados; RC2 usa uma nova geração DSH.",
        "Ajustado para este modelo: {setting} → {value}.",
    ),
    "ru": (
        "Каталог моделей недоступен. Текущая модель: {model}. Повторите /model позже.",
        "Выбор устарел. Откройте /model заново.",
        "Неизвестный провайдер или модель: {model}. Используйте /model.",
        "{model}: проверка выполнения не пройдена ({detail}); выбор не изменён. Выберите исправный маршрут в /model.",
        "{model}: нет явного маршрута. Общее правило подставит другую модель; выбор не изменён. Настройте точный маршрут, а не только псевдоним.",
        "{session}: модель изменена на {model} ({provider}). Сеанс и журналы сохранены; RC2 использует новое поколение DSH.",
        "Настройка для этой модели: {setting} → {value}.",
    ),
    "ar": (
        "كتالوج النماذج غير متاح. النموذج الحالي: {model}. أعد /model لاحقاً.",
        "انتهت صلاحية الاختيار. افتح /model مجدداً.",
        "مزود أو نموذج غير معروف: {model}. استخدم /model.",
        "{model}: فشل اختبار التنفيذ ({detail})؛ الاختيار لم يتغير. اختر مساراً سليماً عبر /model.",
        "{model}: لا يوجد مسار صريح. القاعدة العامة ستستخدم نموذجاً آخر؛ لم يتغير الاختيار. اضبط المسار الدقيق، لا اسماً مستعاراً فقط.",
        "{session}: تغيّر النموذج إلى {model} ({provider}). حُفظت الجلسة والسجلات؛ يستخدم RC2 جيلاً جديداً من DSH.",
        "تعديل لهذا النموذج: {setting} → {value}.",
    ),
    "hi": (
        "मॉडल सूची उपलब्ध नहीं है। वर्तमान मॉडल: {model}। बाद में /model दोहराएँ।",
        "चयन समाप्त हो गया। /model फिर खोलें।",
        "अज्ञात प्रदाता या मॉडल: {model}। /model उपयोग करें।",
        "{model}: निष्पादन जाँच विफल ({detail}); चयन नहीं बदला। /model में सही रूट चुनें।",
        "{model}: स्पष्ट रूट नहीं है। सामान्य नियम दूसरा मॉडल लगाएगा, इसलिए चयन नहीं बदला। केवल उपनाम नहीं, सटीक रूट सेट करें।",
        "{session}: मॉडल {model} ({provider}) पर बदला। सत्र और लॉग सुरक्षित हैं; RC2 नई DSH पीढ़ी उपयोग करता है।",
        "इस मॉडल के लिए बदलाव: {setting} → {value}।",
    ),
    "id": (
        "Katalog model tidak tersedia. Model saat ini: {model}. Coba /model lagi nanti.",
        "Pilihan kedaluwarsa. Buka /model lagi.",
        "Penyedia atau model tidak dikenal: {model}. Gunakan /model.",
        "{model}: uji eksekusi gagal ({detail}); pilihan tidak berubah. Pilih rute sehat di /model.",
        "{model}: tidak ada rute eksplisit. Aturan umum akan mengganti model; pilihan tidak berubah. Atur rute tepat, bukan hanya alias.",
        "{session}: model diubah ke {model} ({provider}). Sesi dan log dipertahankan; RC2 memakai generasi DSH baru.",
        "Disesuaikan untuk model ini: {setting} → {value}.",
    ),
}


def model_text(locale: str, key: str, **values: object) -> str:
    try:
        index = _KEYS.index(key)
    except ValueError as error:
        raise KeyError(key) from error
    return _ROWS[canonical_language(locale)][index].format(**values)


def validate_model_catalog() -> None:
    if set(_ROWS) != set(language_codes()):
        raise ValueError("model locale coverage mismatch")
    for row in _ROWS.values():
        if len(row) != len(_KEYS):
            raise ValueError("model message key mismatch")
        for message, english in zip(row, _ROWS["en"], strict=True):
            fields = {key for _, key, _, _ in Formatter().parse(message) if key is not None}
            expected = {key for _, key, _, _ in Formatter().parse(english) if key is not None}
            if not message or fields != expected:
                raise ValueError("model message placeholder mismatch")
