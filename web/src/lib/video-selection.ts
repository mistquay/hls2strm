const ARRAY_KEYS = ["has_site","lacks_site","model","category","tag","maker","quality"];
const NUMBER_KEYS = ["library_id","added_from","added_to","duration_min","duration_max"];

/** 使用同一份接口筛选参数，分页、排序和展示方式不改变批量范围。 */
export function selectionFilter(query:string):Record<string,unknown> {
  const params = new URLSearchParams(query);
  const result:Record<string,unknown> = {};
  for(const key of new Set(params.keys())) {
    if(["page","size","sort","order"].includes(key)) continue;
    const value = params.get(key)!;
    result[key] = ARRAY_KEYS.includes(key) ? params.getAll(key) : NUMBER_KEYS.includes(key) ? Number(value) : key==="uncensored" ? value==="true" || value==="1" : value;
  }
  return result;
}
