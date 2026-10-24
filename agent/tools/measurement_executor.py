"""Grounded measurement execution. LLM tool arguments never create observations."""
import json
import math
import re

from agent.omni_ad.uncertainty import MeasurementInputError, compute_compliance, result_to_dict
from agent.omni_ad.uncertainty.l3_config import build_compliance_input
from perception.measurement_context import bound_sensor

ALIASES = {
    "mv":("实测值","实测","测得值","测量值","measured_value","mv"),
    "rv":("参考值","参考","标准值","标称值","reference_value","rv"),
    "sp":("器具精度","传感器精度","精度","最大允许误差","sensor_precision","mpe","sp"),
    "temp":("环境温度","温度","env_temp","temp"),
    "h":("湿度","humidity","hum"),
    "ax":("ax","加速度x","x轴加速度"),
    "ay":("ay","加速度y","y轴加速度"),
    "az":("az","加速度z","z轴加速度"),
    "disp":("视差","disparity","disp"),
    "bl":("基线距","基线","baseline","bl"),
    "fl":("焦距","focal_length","fl"),
}
LABELS={"mv":"实测值","rv":"参考值","sp":"器具精度/MPE","temp":"环境温度",
        "h":"湿度","ax":"X轴加速度","ay":"Y轴加速度","az":"Z轴加速度",
        "disp":"视差(px)","bl":"基线(mm)","fl":"焦距(px)"}
NUMBER=r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[+-]?(?:nan|inf(?:inity)?)"
UNIT=r"m/s(?:\^2|²|2)|%rh|mm|毫米|[μµu]m|微米|cm|厘米|inch(?:es)?|in\b|英寸|m\b|米|°c|℃|摄氏度|°f|℉|华氏度|度|px|pixels?|像素|%|％|g\b|[a-zA-Z]+"
NUMERIC_TOOLS={"calc_uncertainty","compliance_judge","comp_3d","read_sensors"}
ARG_KEYS={alias:key for key,aliases in ALIASES.items() for alias in aliases if alias.isascii()}
ARG_KEYS.update({"sensor_precision":"sp","precision":"sp","reference_value":"rv",
                 "measured_value":"mv","env_humidity":"h","t":"temp","focal_length":"fl"})


def infer_task(query):
    query=query or ""
    lower=query.lower()
    has_measurement=bool(re.search(r"实测|测得|测量值|measured_value|\bmv\s*[:=]",lower))
    if not has_measurement and any(w in lower for w in ("解释","什么是","区别","原理","定义","如何","怎么")):
        return None
    if any(w in lower for w in ("公差","usl","lsl")):
        return "product_conformity"
    if any(w in lower for w in ("视差","三维坐标","深度计算")):
        return "comp_3d"
    if any(w in lower for w in ("判定","合规","合格","一致性")) and (has_measurement or re.search(r"\d",lower) or "判定" in lower):
        return "compliance_judge"
    if has_measurement or any(w in lower for w in ("算不确定度","计算不确定度","估算不确定度","不确定度计算","进行不确定度评定","精度评估","算误差","误差计算")):
        return "calc_uncertainty"
    if any(w in lower for w in ("传感器","诊断","加速度","湿度","振动")):
        return "read_sensors"
    return None


def extract_grounded(query):
    values,provenance={},{}
    text=(query or "").replace("−","-")
    for key,aliases in ALIASES.items():
        labels="|".join((r"(?<![a-zA-Z0-9_])"+re.escape(a)+r"(?![a-zA-Z_])") if a.isascii() else re.escape(a)
                        for a in sorted(aliases,key=len,reverse=True))
        pattern=r"(?:"+labels+r")\s*(?:为|是|[:：=])?\s*±?\s*(?P<value>"+NUMBER+r")\s*(?P<unit>"+UNIT+r")?"
        found=[]
        for match in re.finditer(pattern,text,re.I):
            value=float(match.group("value"))
            if not math.isfinite(value):
                raise MeasurementInputError(key,LABELS[key]+"必须是有限数值")
            unit=(match.group("unit") or "").lower()
            tail=text[match.end():].lstrip()
            if tail.startswith(("²","³","/","^","°","㎡","㎥","K")):
                raise MeasurementInputError(key,"单位维度或单位符号不受支持: "+LABELS[key])
            if key=="mv" and re.match(r"^[、,，]\s*[+-]?(?:\d|\.)",tail):
                raise MeasurementInputError(key,"重复测量观测列尚未接入，不能只取第一个数值",status="unsupported")
            if not unit and tail and '\u4e00'<=tail[0]<='\u9fff':
                continuations=tuple(alias for group in ALIASES.values() for alias in group)+("帮","请","判","算","是否","能否","和","与","求")
                if not tail.startswith(continuations):
                    raise MeasurementInputError(key,"数值后的单位或表达无法确认，请明确单位: "+LABELS[key])
            if key in ("mv","rv","sp","bl"):
                factors={"":1,"mm":1,"毫米":1,"um":0.001,"μm":0.001,"µm":0.001,"微米":0.001,
                         "cm":10,"厘米":10,"m":1000,"米":1000,"in":25.4,"inch":25.4,"inches":25.4,"英寸":25.4}
                if unit not in factors:
                    raise MeasurementInputError(key,LABELS[key]+"的单位不受支持: "+unit)
                value*=factors[unit]
                canonical="mm"
            elif key=="temp":
                if unit in ("°f","℉","华氏度"):
                    value=(value-32)*5/9
                elif unit not in ("","°c","℃","度","摄氏度"):
                    raise MeasurementInputError(key,"温度单位不受支持: "+unit)
                canonical="°C"
            elif key in ("disp","fl"):
                if unit not in ("","px","pixel","pixels","像素"):
                    raise MeasurementInputError(key,LABELS[key]+"必须使用像素单位")
                canonical="px"
            elif key=="h":
                if unit not in ("","%","％","%rh"):
                    raise MeasurementInputError(key,"湿度必须使用%RH")
                canonical="%RH"
            else:
                if unit=="g":
                    value*=9.80665
                elif unit not in ("","m/s2","m/s²","m/s^2"):
                    raise MeasurementInputError(key,"加速度单位不受支持: "+unit)
                canonical="m/s2"
            found.append((value,match.group(0),canonical,bool(unit)))
        if found:
            if any(not math.isclose(item[0],found[0][0],rel_tol=1e-12,abs_tol=1e-12) for item in found[1:]):
                raise MeasurementInputError(key,"同一字段出现冲突数值: "+LABELS[key])
            values[key]=found[0][0]
            provenance[key]={"source":"user_text","evidence":found[0][1],"unit":found[0][2],
                             "unit_explicit":found[0][3]}
    vector=re.search(r"(?:加速度|accel)\s*[:：=]?\s*[\[(]?\s*("+NUMBER+r")[,，\s]+("+NUMBER+r")[,，\s]+("+NUMBER+r")\s*[\])]?\s*(g|m/s(?:\^2|²|2))?",text,re.I)
    if vector:
        for key,raw in zip(("ax","ay","az"),vector.groups()[:3]):
            value=float(raw)*(9.80665 if (vector.group(4) or "").lower()=="g" else 1)
            if not math.isfinite(value) or (key in values and not math.isclose(values[key],value,rel_tol=1e-12,abs_tol=1e-12)):
                raise MeasurementInputError(key,"加速度向量无效或与单轴声明冲突")
            values[key]=value
            provenance[key]={"source":"user_text","evidence":vector.group(0),"unit":"m/s2","unit_explicit":bool(vector.group(4))}
    return values,provenance


def _card(payload):
    block="```compliance-card\n"+json.dumps(payload,ensure_ascii=False,allow_nan=False)+"\n```"
    if payload.get("execution_status")!="success":
        return block+"\n\n未执行评定："+payload["error"]["message"]
    record="数值记录：实测={:.8g} mm，U={:.8g} mm。".format(payload["measured_value"],payload["U"])
    if payload.get("assessment_type")=="uncertainty_only":
        return block+"\n\n"+record+"本次仅估计预算，不执行一致性或工件符合性判定。"
    verdict={"pass":"满足工程规则","fail":"不一致","indeterminate":"无法确定"}[payload["verdict"]]
    return block+"\n\n"+record+"参考={:.8g} mm；工程参考一致性：{}。测量风险未评估。".format(payload["reference_value"],verdict)


def execute_tool(name,query,measurement_context=None,args=None):
    context=measurement_context or {}
    try:
        if name=="product_conformity":
            raise MeasurementInputError("assessment_type","当前未实现基于公差的产品符合性评定；不能用参考一致性代替",status="unsupported")
        values,provenance=extract_grounded(query)
        if args is not None and not isinstance(args,dict):
            raise MeasurementInputError("tool_args","工具参数必须是对象")
        sensor=bound_sensor(context)
        if name=="read_sensors" and sensor and not any(k in values for k in ("temp","h","ax","ay","az")):
            values.update({"temp":sensor["t"],**{k:sensor[k] for k in ("h","ax","ay","az")}})
            provenance.update({key:{"source":"bound_sensor","event_id":context.get("event_id")}
                               for key in ("temp","h","ax","ay","az")})
        elif name!="read_sensors" and sensor and "temp" not in values:
            values["temp"]=sensor["t"]
            provenance["temp"]={"source":"bound_sensor","event_id":context.get("event_id"),"unit":"°C"}
        for key,value in (args or {}).items():
            field=ARG_KEYS.get(key)
            if field is None:
                continue
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
                raise MeasurementInputError(field,"路由参数不是有效数值: "+field)
            if field not in values or not math.isclose(float(value),values[field],rel_tol=1e-10,abs_tol=1e-12):
                raise MeasurementInputError(field,"路由参数与用户原文不一致，暂不计算: "+LABELS[field])
            values[field]=float(value)  # consume the checked tool argument
            provenance[field]["route_argument_verified"]=True
        required={"compliance_judge":("mv","rv","sp","temp"),"calc_uncertainty":("mv","sp","temp"),
                  "comp_3d":("disp","bl","fl"),"read_sensors":("temp","h","ax","ay","az")}
        missing=[key for key in required[name] if key not in values]
        if missing:
            raise MeasurementInputError(missing[0],"缺少必需参数，暂不计算："+"、".join(LABELS[k] for k in missing),status="needs_input")
        if name=="comp_3d":
            from tools.measurement_tools import calculate_3d_coordinates
            if any(values[k]<=0 for k in required[name]):
                raise MeasurementInputError("disp","视差、基线和焦距必须为正数")
            return calculate_3d_coordinates(values["disp"],values["bl"],values["fl"])
        if name=="read_sensors":
            from tools.measurement_tools import sensor_data_diagnose
            if not -273.15<=values["temp"] or not 0<=values["h"]<=100:
                raise MeasurementInputError("sensor","温湿度数值超出允许范围")
            output=sensor_data_diagnose(values["temp"],values["h"],values["ax"],values["ay"],values["az"])
            from_snapshot=all(provenance.get(k,{}).get("source")=="bound_sensor" for k in ("temp","h","ax","ay","az"))
            payload={"temp":values["temp"],"hum":values["h"],"vib":math.hypot(values["ax"],values["ay"],values["az"]),
                     "execution_status":"success","source":"bound_sensor" if from_snapshot else "user_text",
                     "event_id":context.get("event_id")}
            return "```sensor-data\n"+json.dumps(payload,ensure_ascii=False,allow_nan=False)+"\n```\n"+output
        material_match=re.search(r"(?:材料|material)\s*(?:[:：=]|为|是)\s*([^\s，,。;；]+)",query,re.I)
        material_args={"material":material_match.group(1),"temp_coefficient":None} if material_match else {}
        inp=build_compliance_input(values["mv"],values.get("rv") if name=="compliance_judge" else None,
                                   values["sp"],env_temp=values["temp"],include_u_ood=False,
                                   assessment_type="reference_consistency" if name=="compliance_judge" else "uncertainty_only",**material_args)
        result=compute_compliance(inp)
        payload=result_to_dict(result)
        payload.update(measured_value=values["mv"],reference_value=values.get("rv") if name=="compliance_judge" else None,
                       k=inp.k,critical=result.requires_human_review,review=result.reason,
                       event_id=context.get("event_id"),input_provenance=provenance,
                       measurement_context=context,visual_used=False,sensor_used=False,
                       risk_execution_status="not_assessed",ood_policy="disabled_unvalidated_mapping",
                       method_status="engineering_prototype",physical_validation="not_validated",
                       material=inp.material,temp_coefficient=inp.temp_coefficient,
                       measured_value_source="user_text",measurement_time_known=False,
                       sensor_environment_used=provenance.get("temp",{}).get("source")=="bound_sensor")
        if name=="compliance_judge":
            payload["decision_bounds"]={"pass_abs_mm":min(inp.critical_band_low*result.U_pass,inp.critical_band_high*result.U_base),
                                        "fail_abs_mm":inp.critical_band_high*result.U_base}
        try:
            from compliance.gate import audit_verdict
            audit_verdict(result.verdict or "not_applicable",f"event={context.get('event_id')} task={name} U={result.U:.6g} ood=disabled_unvalidated")
        except Exception:
            pass
        return _card(payload)
    except MeasurementInputError as exc:
        return _card({**exc.to_dict(),"task":name,"event_id":context.get("event_id")})
    except (ValueError,TypeError,ArithmeticError) as exc:
        error=MeasurementInputError("calculation","计算参数或结果无效: "+str(exc))
        return _card({**error.to_dict(),"task":name,"event_id":context.get("event_id")})


def execute_request(query,measurement_context=None,router_decision=None):
    decision=router_decision or {}
    calls=[c for c in decision.get("tool_calls") or [] if isinstance(c,dict) and c.get("name") in NUMERIC_TOOLS]
    inferred=infer_task(query)
    if inferred in ("compliance_judge","calc_uncertainty","comp_3d"):
        calls=[c for c in calls if c["name"]!="read_sensors"]
    if inferred=="product_conformity":
        calls=[{"name":inferred,"args":{}}]
    elif inferred=="compliance_judge" and not any(c["name"]==inferred for c in calls):
        calls=[{"name":inferred,"args":{}}]
    elif not calls and inferred:
        calls=[{"name":inferred,"args":{}}]
    if not calls:
        return None
    # A request can contain several independent calculations; preserve every result.
    return "\n\n".join(execute_tool(c["name"],query,measurement_context,c.get("args")) for c in calls)
