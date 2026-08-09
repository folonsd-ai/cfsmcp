"""Map 1C XML-dump BSL paths to report parent paths and method object paths."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.services.kinds import KIND_RU_TO_EN

# English dump folder -> Russian plural (report first segment)
_EN_TO_RU: dict[str, str] = {}
for ru, en in KIND_RU_TO_EN.items():
    _EN_TO_RU.setdefault(en, ru)

# Extra aliases seen in dumps
_EN_TO_RU.update(
    {
        "Catalogs": "Справочники",
        "Documents": "Документы",
        "Enums": "Перечисления",
        "Reports": "Отчеты",
        "DataProcessors": "Обработки",
        "InformationRegisters": "РегистрыСведений",
        "AccumulationRegisters": "РегистрыНакопления",
        "AccountingRegisters": "РегистрыБухгалтерии",
        "CalculationRegisters": "РегистрыРасчета",
        "ChartsOfCharacteristicTypes": "ПланыВидовХарактеристик",
        "ChartsOfAccounts": "ПланыСчетов",
        "ChartsOfCalculationTypes": "ПланыВидовРасчета",
        "ExchangePlans": "ПланыОбмена",
        "BusinessProcesses": "БизнесПроцессы",
        "Tasks": "Задачи",
        "DocumentJournals": "ЖурналыДокументов",
        "Constants": "Константы",
        "CommonModules": "ОбщиеМодули",
        "CommonForms": "ОбщиеФормы",
        "CommonCommands": "ОбщиеКоманды",
        "CommonTemplates": "ОбщиеМакеты",
        "CommonPictures": "ОбщиеКартинки",
        "CommandGroups": "ГруппыКоманд",
        "Roles": "Роли",
        "Subsystems": "Подсистемы",
        "SessionParameters": "ПараметрыСеанса",
        "Languages": "Языки",
        "DefinedTypes": "ОпределяемыеТипы",
        "FilterCriteria": "КритерииОтбора",
        "FunctionalOptions": "ФункциональныеОпции",
        "FunctionalOptionsParameters": "ПараметрыФункциональныхОпций",
        "SettingsStorages": "ХранилищаНастроек",
        "StyleItems": "ЭлементыСтиля",
        "Styles": "Стили",
        "Interfaces": "Интерфейсы",
        "XDTOPackages": "ПакетыXDTO",
        "WebServices": "WebСервисы",
        "HTTPServices": "HTTPСервисы",
        "WSReferences": "WSСсылки",
        "EventSubscriptions": "ПодпискиНаСобытия",
        "ScheduledJobs": "РегламентныеЗадания",
        "Bots": "Боты",
        "ExternalDataSources": "ВнешниеИсточникиДанных",
    }
)

_MODULE_FILES = {
    "Module.bsl": "Module",
    "ObjectModule.bsl": "ObjectModule",
    "ManagerModule.bsl": "ManagerModule",
    "RecordSetModule.bsl": "RecordSetModule",
    "ValueManagerModule.bsl": "ValueManagerModule",
    "CommandModule.bsl": "CommandModule",
}

# Config-root modules: Ext/<File>.bsl (Mac/Windows dump)
_ROOT_MODULE_FILES = {
    "Module.bsl": "Module",
    "ManagedApplicationModule.bsl": "ManagedApplicationModule",
    "OrdinaryApplicationModule.bsl": "OrdinaryApplicationModule",
    "SessionModule.bsl": "SessionModule",
    "ExternalConnectionModule.bsl": "ExternalConnectionModule",
}


@dataclass(frozen=True)
class BslModuleRef:
    """Resolved location of a BSL module relative to metadata report paths."""

    parent_path: str
    module_role: str
    source_file: str


def normalize_zip_member(name: str) -> str:
    """Normalize separators and Unicode (NFC) so Mac NFD paths match report names."""
    n = unicodedata.normalize("NFC", name.replace("\\", "/").lstrip("/"))
    while "//" in n:
        n = n.replace("//", "/")
    return n


def strip_common_root(members: list[str]) -> tuple[str, list[str]]:
    """If all paths share a single top folder, strip it (zip of a directory)."""
    norms = [normalize_zip_member(m) for m in members if m and not m.endswith("/")]
    if not norms:
        return "", []
    tops = {p.split("/", 1)[0] for p in norms}
    if len(tops) == 1:
        root = next(iter(tops))
        # Only strip if it looks like a config folder, not a type folder
        if root not in _EN_TO_RU and root not in {"Ext", "Forms", "Commands"}:
            stripped = []
            for p in norms:
                rest = p.split("/", 1)
                stripped.append(rest[1] if len(rest) == 2 else p)
            return root, stripped
    return "", norms


def resolve_bsl_module(zip_path: str) -> BslModuleRef | None:
    """Map dump-relative path of a .bsl file to report parent_path + module role."""
    p = normalize_zip_member(zip_path)
    if not p.lower().endswith(".bsl"):
        return None
    parts = p.split("/")

    # Ext/SessionModule.bsl (and other config-root modules)
    if len(parts) == 2 and parts[0] == "Ext":
        role = _ROOT_MODULE_FILES.get(parts[1])
        if role:
            # Parent is the Configuration node: Конфигурации.<Name> — filled by caller
            # via sentinel that ingest resolves against parent_paths.
            return BslModuleRef(
                parent_path="__CONFIGURATION__",
                module_role=role,
                source_file=p,
            )

    if len(parts) < 3:
        return None

    # .../Forms/{Form}/Ext/Form/Module.bsl
    if "Forms" in parts:
        try:
            fi = parts.index("Forms")
            type_en = parts[0]
            obj_name = parts[1]
            form_name = parts[fi + 1]
        except (ValueError, IndexError):
            return None
        ru = _EN_TO_RU.get(type_en)
        if not ru or fi + 1 >= len(parts):
            return None
        parent = f"{ru}.{obj_name}.Формы.{form_name}"
        return BslModuleRef(parent_path=parent, module_role="Form", source_file=p)

    # .../Commands/{Cmd}/Ext/CommandModule.bsl (or Module.bsl)
    if "Commands" in parts and parts[0] not in {"CommonCommands", "CommandGroups"}:
        try:
            ci = parts.index("Commands")
            type_en = parts[0]
            obj_name = parts[1]
            cmd_name = parts[ci + 1]
        except (ValueError, IndexError):
            return None
        ru = _EN_TO_RU.get(type_en)
        if not ru:
            return None
        parent = f"{ru}.{obj_name}.Команды.{cmd_name}"
        fname = parts[-1]
        role = _MODULE_FILES.get(fname, "CommandModule")
        return BslModuleRef(parent_path=parent, module_role=role, source_file=p)

    # {Type}/{Name}/Ext/{ModuleFile}.bsl
    if len(parts) >= 4 and parts[-2] == "Ext":
        type_en, obj_name, fname = parts[0], parts[1], parts[-1]
        ru = _EN_TO_RU.get(type_en)
        role = _MODULE_FILES.get(fname)
        if not ru or not role:
            return None
        # CommonForms: Ext/Form/Module.bsl handled above via Forms; CommonForm often Ext/Form/Module
        if fname == "Module.bsl" and len(parts) >= 5 and parts[-3] == "Form":
            parent = f"{ru}.{obj_name}"
            return BslModuleRef(parent_path=parent, module_role="Form", source_file=p)
        parent = f"{ru}.{obj_name}"
        return BslModuleRef(parent_path=parent, module_role=role, source_file=p)

    # CommonForms/Name/Ext/Form/Module.bsl without "Forms" segment duplication
    if len(parts) >= 5 and parts[2] == "Ext" and parts[3] == "Form":
        type_en, obj_name = parts[0], parts[1]
        ru = _EN_TO_RU.get(type_en)
        if not ru:
            return None
        return BslModuleRef(parent_path=f"{ru}.{obj_name}", module_role="Form", source_file=p)

    return None


def explain_unresolved_bsl_path(zip_path: str) -> str:
    """Why resolve_bsl_module returned None — for ingest diagnostics."""
    p = normalize_zip_member(zip_path)
    bits: list[str] = [f"file={p}"]
    if not p.lower().endswith(".bsl"):
        bits.append("reason=not_bsl_extension")
        return " | ".join(bits)
    parts = p.split("/")
    bits.append(f"segments={len(parts)}")
    bits.append(f"parts={parts[:8]!r}" + ("…" if len(parts) > 8 else ""))
    if len(parts) == 2 and parts[0] == "Ext":
        bits.append(
            f"reason=unknown_root_module_file | file={parts[1]!r} | "
            f"known={sorted(_ROOT_MODULE_FILES)}"
        )
        return " | ".join(bits)
    if len(parts) < 3:
        bits.append("reason=too_few_path_segments (need Type/Name/…)")
        return " | ".join(bits)

    type_en = parts[0]
    ru = _EN_TO_RU.get(type_en)
    if ru is None:
        bits.append(f"reason=unknown_type_folder | type={type_en!r}")
        bits.append("hint=expected English dump root (Documents, Catalogs, CommonModules, …)")
        # Cyrillic root?
        if any("\u0400" <= ch <= "\u04FF" for ch in type_en):
            bits.append("hint=path_looks_russian_not_english_dump")
        return " | ".join(bits)

    bits.append(f"type_ok={type_en}->{ru}")

    if "Forms" in parts:
        fi = parts.index("Forms")
        if fi + 1 >= len(parts):
            bits.append("reason=Forms_without_form_name")
        else:
            bits.append(
                f"reason=Forms_branch_incomplete | form={parts[fi + 1]!r} | "
                f"tail={parts[fi:]!r}"
            )
        return " | ".join(bits)

    if "Commands" in parts and type_en not in {"CommonCommands", "CommandGroups"}:
        bits.append("reason=Commands_branch_incomplete")
        return " | ".join(bits)

    if len(parts) >= 4 and parts[-2] == "Ext":
        fname = parts[-1]
        role = _MODULE_FILES.get(fname)
        if not role:
            bits.append(
                f"reason=unknown_module_file | file={fname!r} | "
                f"known={sorted(_MODULE_FILES)}"
            )
        else:
            bits.append(f"reason=ext_branch_unmatched | role={role}")
        return " | ".join(bits)

    if len(parts) >= 5 and parts[2] == "Ext" and parts[3] == "Form":
        bits.append("reason=common_form_pattern_unmatched")
        return " | ".join(bits)

    bits.append("reason=no_matching_pattern")
    bits.append(
        "hint=need …/Ext/ObjectModule.bsl or …/Forms/Name/Ext/Form/Module.bsl "
        "(or Form.bin → synthetic Module.bsl)"
    )
    return " | ".join(bits)


def method_object_path(parent_path: str, module_role: str, method_name: str) -> str:
    return f"{parent_path}.Методы.{module_role}.{method_name}"
