"""Short actionable delivery-check notices; never display untrusted evidence text."""
# ruff: noqa: E501, RUF001
from __future__ import annotations

from xdsh.core.events import RESEARCH_ADVISORIES, VERIFICATION_ISSUES, VerificationEvent
from xdsh.core.i18n import canonical_language

# Title, next action, then labels in VERIFICATION_ISSUES order.
_ROWS = {
    "zh-CN": ("交付验收未通过", "已有结果保留。在本会话发送“继续”，先修复这些检查再核验；不会自动重跑整项任务。",
              "检查清单缺失或无效", "实际参数或调用记录", "报告字段", "数值或比例", "数组维度、范围或退化", "输入或清单被改动", "文件缺失或不可读"),
    "zh-TW": ("交付驗收未通過", "已有結果保留。在本會話傳送「繼續」，先修復這些檢查再核驗；不會自動重跑整項任務。",
              "檢查清單缺失或無效", "實際參數或呼叫紀錄", "報告欄位", "數值或比例", "陣列維度、範圍或退化", "輸入或清單被改動", "檔案缺失或不可讀"),
    "en": ("Delivery checks did not pass", "Existing output is retained. Send continue here to fix these checks and verify again; the whole task is not automatically replayed.",
           "missing or invalid checklist", "effective parameters or call record", "report fields", "numbers or ratios", "array shape, range or degeneracy", "changed input or checklist", "missing or unreadable files"),
    "ja": ("成果物の検証に失敗しました", "既存の結果は保持されています。この会話で「続けて」と送り、問題を修正して再検証してください。タスク全体は自動再実行しません。",
           "確認リストの欠落・不備", "実際の引数・呼出記録", "報告の項目", "数値・比率", "配列の次元・範囲・退化", "入力・確認リストの変更", "ファイルの欠落・読取不可"),
    "ko": ("결과물 검증을 통과하지 못했습니다", "기존 결과는 보존됩니다. 이 대화에 계속을 보내 문제를 수정하고 다시 검증하세요. 전체 작업을 자동 반복하지 않습니다.",
           "검사 목록 누락 또는 오류", "실제 매개변수 또는 호출 기록", "보고서 필드", "수치 또는 비율", "배열 차원·범위·퇴화", "입력 또는 목록 변경", "파일 누락 또는 읽기 불가"),
    "es": ("La entrega no superó las comprobaciones", "Los resultados se conservan. Envía continuar aquí para corregir y verificar; no se repite automáticamente toda la tarea.",
           "lista ausente o inválida", "parámetros efectivos o registro de llamada", "campos del informe", "valores o proporciones", "dimensiones, rango o degeneración", "entrada o lista modificada", "archivos ausentes o ilegibles"),
    "fr": ("Les vérifications de livraison ont échoué", "Les résultats sont conservés. Envoyez continuer ici pour corriger puis vérifier ; la tâche entière n’est pas relancée automatiquement.",
           "liste absente ou invalide", "paramètres effectifs ou appel", "champs du rapport", "nombres ou rapports", "dimensions, plage ou dégénérescence", "entrée ou liste modifiée", "fichiers absents ou illisibles"),
    "de": ("Ergebnisprüfung nicht bestanden", "Vorhandene Ergebnisse bleiben erhalten. Senden Sie hier weiter, um die Prüfungen zu korrigieren. Kein automatischer Neustart der ganzen Aufgabe.",
           "fehlende oder ungültige Prüfliste", "effektive Parameter oder Aufrufprotokoll", "Berichtsfelder", "Zahlen oder Verhältnisse", "Arrayform, Wertebereich oder Degeneration", "geänderte Eingabe oder Prüfliste", "fehlende oder unlesbare Dateien"),
    "pt-BR": ("A entrega não passou nas verificações", "Os resultados foram preservados. Envie continuar aqui para corrigir e verificar novamente; a tarefa inteira não é repetida automaticamente.",
              "lista ausente ou inválida", "parâmetros efetivos ou registro de chamada", "campos do relatório", "números ou proporções", "dimensões, limites ou degeneração", "entrada ou lista alterada", "arquivos ausentes ou ilegíveis"),
    "ru": ("Проверка результатов не пройдена", "Результаты сохранены. Отправьте здесь «продолжить», чтобы исправить и проверить. Вся задача не запускается повторно автоматически.",
           "нет списка проверок или он неверен", "фактические параметры или запись вызова", "поля отчёта", "числа или отношения", "размерность, диапазон или вырождение", "изменены входные данные или список", "нет файлов или они недоступны"),
    "ar": ("لم تجتز النتائج التحقق", "النتائج محفوظة. أرسل متابعة هنا لإصلاح الفحوص والتحقق مجدداً؛ لن تُعاد المهمة كاملة تلقائياً.",
           "قائمة فحص مفقودة أو غير صالحة", "معاملات فعلية أو سجل استدعاء", "حقول التقرير", "أرقام أو نسب", "أبعاد المصفوفة أو نطاقها أو انحلالها", "تغيّر الإدخال أو القائمة", "ملفات مفقودة أو غير مقروءة"),
    "hi": ("परिणाम जाँच सफल नहीं हुई", "मौजूदा परिणाम सुरक्षित हैं। सुधार और पुनः जाँच के लिए यहाँ जारी रखें भेजें; पूरा कार्य अपने आप दोहराया नहीं जाएगा।",
           "जाँच सूची अनुपस्थित या अमान्य", "वास्तविक पैरामीटर या कॉल रिकॉर्ड", "रिपोर्ट फ़ील्ड", "संख्याएँ या अनुपात", "ऐरे आकार, सीमा या स्थिर मान", "बदला इनपुट या सूची", "अनुपस्थित या अपठनीय फ़ाइलें"),
    "id": ("Pemeriksaan hasil belum lulus", "Hasil tersimpan. Kirim lanjut di sini untuk memperbaiki lalu memeriksa lagi; seluruh tugas tidak diulang otomatis.",
           "daftar pemeriksaan hilang atau tidak valid", "parameter efektif atau catatan panggilan", "kolom laporan", "angka atau rasio", "bentuk, rentang, atau degenerasi larik", "masukan atau daftar berubah", "berkas hilang atau tidak terbaca"),
}


def verification_notice(language: str, issues: tuple[str, ...], *, brief: bool = False) -> str:
    row = _ROWS[canonical_language(language)]
    labels = [row[2 + VERIFICATION_ISSUES.index(issue)] for issue in dict.fromkeys(issues)
              if issue in VERIFICATION_ISSUES]
    title = row[0] + (": " + "; ".join(labels) if labels else "")
    return title if brief else title + "\n" + row[1]


_REPAIR = {
    "zh-CN": "正在修复交付检查 {attempt}/{limit} · 已有成果保留",
    "zh-TW": "正在修復交付檢查 {attempt}/{limit} · 已有成果保留",
    "en": "Repairing delivery checks {attempt}/{limit} · existing results retained",
    "ja": "成果物の検証を修正中 {attempt}/{limit} · 既存結果は保持",
    "ko": "결과물 검사 수정 중 {attempt}/{limit} · 기존 결과 보존",
    "es": "Corrigiendo la entrega {attempt}/{limit} · resultados conservados",
    "fr": "Correction des résultats {attempt}/{limit} · résultats conservés",
    "de": "Ergebnisprüfung korrigieren {attempt}/{limit} · Ergebnisse erhalten",
    "pt-BR": "Corrigindo a entrega {attempt}/{limit} · resultados preservados",
    "ru": "Исправление проверки {attempt}/{limit} · результаты сохранены",
    "ar": "إصلاح فحوص النتائج {attempt}/{limit} · النتائج محفوظة",
    "hi": "परिणाम जाँच सुधार {attempt}/{limit} · मौजूदा परिणाम सुरक्षित",
    "id": "Memperbaiki pemeriksaan {attempt}/{limit} · hasil tersimpan",
}


def verification_repair_notice(language: str, event: VerificationEvent) -> str:
    return _REPAIR[canonical_language(language)].format(
        attempt=event.attempt, limit=event.max_attempts)


# Title/caveat then content-free labels in RESEARCH_ADVISORIES order.
_RESEARCH = {
    "zh-CN": ("科研提示 · 交付通过不等于完整复现", "科学对照存在差异", "仅声明子集", "覆盖范围未确认", "资源估算不完整", "估算内存存在风险", "执行策略未定"),
    "zh-TW": ("科研提示 · 交付通過不等於完整復現", "科學對照存在差異", "僅聲明子集", "覆蓋範圍未確認", "資源估算不完整", "估算記憶體存在風險", "執行策略未定"),
    "en": ("Research notes · delivery checks do not prove full reproduction", "scientific comparison differs", "subset declared", "coverage unconfirmed", "resource estimates incomplete", "estimated memory risk", "execution strategy undecided"),
    "ja": ("研究上の注意 · 検証合格は完全な再現の証明ではありません", "科学的比較に差異", "部分集合のみ申告", "対象範囲未確認", "資源見積りが不十分", "メモリ見積りにリスク", "実行方針未定"),
    "ko": ("연구 참고 · 납품 검사 통과는 완전한 재현의 증거가 아닙니다", "과학적 비교에 차이", "일부 데이터만 선언됨", "범위 미확인", "자원 추정 불완전", "예상 메모리 위험", "실행 전략 미정"),
    "es": ("Notas de investigación · las comprobaciones no prueban una reproducción completa", "diferencia científica", "subconjunto declarado", "cobertura sin confirmar", "estimaciones de recursos incompletas", "riesgo de memoria estimado", "estrategia sin decidir"),
    "fr": ("Notes de recherche · les contrôles ne prouvent pas une reproduction complète", "écart scientifique", "sous-ensemble déclaré", "couverture non confirmée", "estimations de ressources incomplètes", "risque mémoire estimé", "stratégie non définie"),
    "de": ("Forschungshinweise · Prüfungen belegen keine vollständige Reproduktion", "wissenschaftliche Abweichung", "Teilmenge angegeben", "Umfang unbestätigt", "Ressourcenschätzung unvollständig", "geschätztes Speicherrisiko", "Ausführungsstrategie offen"),
    "pt-BR": ("Notas de pesquisa · as verificações não provam reprodução completa", "diferença científica", "subconjunto declarado", "cobertura não confirmada", "estimativas de recursos incompletas", "risco estimado de memória", "estratégia indefinida"),
    "ru": ("Замечания · проверки не доказывают полное воспроизведение", "научное расхождение", "заявлено подмножество", "охват не подтверждён", "неполная оценка ресурсов", "оценочный риск памяти", "стратегия не определена"),
    "ar": ("ملاحظات بحثية · اجتياز الفحوص لا يثبت إعادة إنتاج كاملة", "اختلاف في المقارنة العلمية", "مجموعة فرعية معلنة", "النطاق غير مؤكد", "تقديرات الموارد غير مكتملة", "خطر ذاكرة تقديري", "استراتيجية التنفيذ غير محددة"),
    "hi": ("शोध टिप्पणी · जाँच पास होना पूर्ण पुनरुत्पादन का प्रमाण नहीं", "वैज्ञानिक तुलना में अंतर", "केवल उपसमूह घोषित", "दायरा अपुष्ट", "संसाधन अनुमान अधूरे", "अनुमानित मेमोरी जोखिम", "निष्पादन रणनीति अनिर्धारित"),
    "id": ("Catatan riset · pemeriksaan tidak membuktikan reproduksi penuh", "perbandingan ilmiah berbeda", "subset dinyatakan", "cakupan belum dikonfirmasi", "estimasi sumber daya belum lengkap", "perkiraan risiko memori", "strategi eksekusi belum ditentukan"),
}


def research_advisory_notice(language: str, issues: tuple[str, ...]) -> str:
    row = _RESEARCH[canonical_language(language)]
    labels = [row[1 + RESEARCH_ADVISORIES.index(issue)] for issue in dict.fromkeys(issues)
              if issue in RESEARCH_ADVISORIES]
    return row[0] + ("\n" + " · ".join(labels) if labels else "")
