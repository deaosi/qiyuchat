/** Deterministic, fictional preview data. No customer records are used. */
export interface QAPair {
  id: string
  sessionId: string
  clusterId: number
  question: string
  answer: string
  customerName: string
  customerId: string
  groupName: string
  staffName: string
  /** ISO timestamp with the original demo's Asia/Shanghai offset. */
  askedAt: string
  archived: boolean
}

export interface Topic {
  id: number
  title: string
  n: number
  q: readonly string[]
  a: string
}

export type DemoDays = 1 | 3 | 7
export type ReplyFilter = "" | "all" | "answered" | "unanswered"

export interface DemoFilters {
  source?: "demo"
  days?: DemoDays
  group?: string
  staff?: string
  q?: string
  replied?: ReplyFilter
  topic?: number | string
  customer?: string
  day?: string
}

export interface TopicAggregate {
  id: number
  clusterId: number
  title: string
  question: string
  total: number
  /** Alias matching the existing API's question-cluster count. */
  count: number
  answered: number
  unanswered: number
  customers: number
  groups: number
  variants: number
  samples: string[]
  share: number
  firstSeen: string
  lastSeen: string
}

export interface TrendPoint {
  date: string
  answered: number
  unanswered: number
  total: number
}

export const DEMO_END_DATE = "2026-11-09"

export const groups = [
  "旗舰店客户群",
  "新品体验群",
  "会员服务群",
  "售后服务群",
] as const

export const staffNames = ["小林", "小陈", "小周"] as const

export const topics: readonly Topic[] = [
  {
    id: 101,
    title: "发货时间与配送范围",
    n: 52,
    q: ["今天下单什么时候能发货？", "一般几天可以送到？", "这个地址可以配送吗？"],
    a: "您好，样例商品通常在付款后 48 小时内发出，配送时效以订单页显示为准。",
  },
  {
    id: 102,
    title: "退换货条件与流程",
    n: 42,
    q: ["尺寸不合适可以换吗？", "退货要保留哪些东西？", "七天内可以申请退货吗？"],
    a: "您好，符合商品页面退换条件时，可从订单详情申请售后，并保留商品及包装。具体以订单售后说明为准。",
  },
  {
    id: 103,
    title: "发票申请与开具",
    n: 31,
    q: ["可以开电子发票吗？", "发票抬头在哪里填写？", "下单后还能补开发票吗？"],
    a: "您好，可在订单详情的发票入口填写抬头和邮箱，按页面提示提交开票申请。",
  },
  {
    id: 104,
    title: "尺码与商品规格",
    n: 26,
    q: ["这款应该选择什么规格？", "两个型号有什么区别？", "有尺寸对照表吗？"],
    a: "您好，可以参考商品详情页的规格对照表。您也可以提供使用场景，我们协助核对合适的型号。",
  },
  {
    id: 105,
    title: "优惠券使用条件",
    n: 20,
    q: ["这张优惠券为什么不能用？", "优惠券可以叠加使用吗？", "满减活动什么时候结束？"],
    a: "您好，请核对优惠券适用商品、使用门槛和有效期，最终优惠以结算页显示为准。",
  },
  {
    id: 106,
    title: "安装与使用说明",
    n: 17,
    q: ["收到后怎么安装？", "可以发一下使用说明吗？", "第一次使用需要注意什么？"],
    a: "您好，包装内附有安装说明，也可以在商品详情页查看步骤。操作前请先阅读使用注意事项。",
  },
  {
    id: 107,
    title: "物流状态查询",
    n: 14,
    q: ["物流两天没更新了怎么办？", "包裹现在到哪里了？", "显示签收但没有收到怎么办？"],
    a: "您好，建议先查看订单物流详情，如状态持续未更新，可以联系订单客服进一步核实。",
  },
  {
    id: 108,
    title: "保修范围与期限",
    n: 10,
    q: ["这个商品保修多久？", "保修需要提供什么凭证？", "人为损坏可以维修吗？"],
    a: "您好，保修范围和期限以商品页面及保修卡为准，申请时请准备订单信息与故障说明。",
  },
]

const dayPool = [0, 0, 1, 2, 3, 3, 4, 4, 4, 5, 5, 6, 6, 6]
let serial = 0

// Keep the original preview's 212 pairs, dates, answers and identities exactly.
export const demoPairs: QAPair[] = topics
  .flatMap((topic, topicIndex) =>
    Array.from({ length: topic.n }, (_, questionIndex): QAPair => {
      const index = serial++
      const day = dayPool[(index + topicIndex * 3) % dayPool.length]
      const customerIndex = (index * 7 + topicIndex) % 24
      const customerNumber = String(customerIndex + 1).padStart(3, "0")
      const date = `2026-11-${String(day + 3).padStart(2, "0")}`
      const hour = String(9 + (index % 6)).padStart(2, "0")
      const minute = String((index * 7) % 60).padStart(2, "0")

      return {
        id: `QA-${String(index + 1).padStart(4, "0")}`,
        sessionId: `DEMO-S${7000 + index}`,
        clusterId: topic.id,
        question: topic.q[questionIndex % topic.q.length],
        answer: index % 13 === 0 ? "" : topic.a,
        customerName: `客户 ${customerNumber}`,
        customerId: `DEMO-C${customerNumber}`,
        groupName: groups[customerIndex % groups.length],
        staffName: staffNames[(index + topicIndex) % staffNames.length],
        askedAt: `${date}T${hour}:${minute}:00+08:00`,
        archived: index % 17 === 0,
      }
    }),
  )
  .sort((a, b) => b.askedAt.localeCompare(a.askedAt))

function dayOf(pair: QAPair): string {
  return pair.askedAt.slice(0, 10)
}

function hasAnswer(pair: QAPair): boolean {
  return pair.answer.trim().length > 0
}

function selectedDays(days: number): DemoDays {
  return days === 1 || days === 3 ? days : 7
}

function dayOffset(offset: number): string {
  // Date-only arithmetic in UTC avoids the viewer's local timezone changing days.
  const date = new Date(`${DEMO_END_DATE}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + offset)
  return date.toISOString().slice(0, 10)
}

/** All conditions are combined; the default scope is the seven-day demo. */
export function filterPairs(
  filters: DemoFilters = {},
  pairs: readonly QAPair[] = demoPairs,
): QAPair[] {
  const firstDay = dayOffset(1 - selectedDays(filters.days ?? 7))
  const query = filters.q?.trim().toLocaleLowerCase("zh-CN") ?? ""
  const topicId = filters.topic ? Number(filters.topic) : 0

  return pairs.filter((pair) => {
    const day = dayOf(pair)
    if (day < firstDay || day > DEMO_END_DATE) return false
    if (filters.day && day !== filters.day) return false
    if (filters.group && pair.groupName !== filters.group) return false
    if (filters.staff && pair.staffName !== filters.staff) return false
    if (filters.topic && pair.clusterId !== topicId) return false
    if (filters.customer && pair.customerId !== filters.customer) return false
    if (filters.replied === "answered" && !hasAnswer(pair)) return false
    if (filters.replied === "unanswered" && hasAnswer(pair)) return false
    if (
      query &&
      ![pair.question, pair.answer, pair.customerName, pair.sessionId]
        .join(" ")
        .toLocaleLowerCase("zh-CN")
        .includes(query)
    ) {
      return false
    }
    return true
  })
}

/** Aggregate only the supplied scope, matching the API's visible-cluster logic. */
export function aggregateTopics(pairs: readonly QAPair[]): TopicAggregate[] {
  const buckets = new Map<number, QAPair[]>()
  for (const pair of pairs) {
    const bucket = buckets.get(pair.clusterId) ?? []
    bucket.push(pair)
    buckets.set(pair.clusterId, bucket)
  }

  return Array.from(buckets, ([clusterId, members]): TopicAggregate => {
    const topic = topics.find((item) => item.id === clusterId)
    const counts = new Map<string, number>()
    for (const pair of members) {
      counts.set(pair.question, (counts.get(pair.question) ?? 0) + 1)
    }
    const variants = Array.from(counts.keys()).sort(
      (a, b) =>
        (counts.get(b) ?? 0) - (counts.get(a) ?? 0) ||
        a.length - b.length ||
        a.localeCompare(b, "zh-CN"),
    )
    const days = members.map(dayOf).sort()
    const answered = members.filter(hasAnswer).length

    return {
      id: clusterId,
      clusterId,
      title: topic?.title ?? `问题簇 ${clusterId}`,
      question: variants[0] ?? "",
      total: members.length,
      count: members.length,
      answered,
      unanswered: members.length - answered,
      customers: new Set(members.map((pair) => pair.customerId)).size,
      groups: new Set(members.map((pair) => pair.groupName)).size,
      variants: variants.length,
      samples: variants.slice(0, 5),
      share: pairs.length ? Math.round((members.length / pairs.length) * 1000) / 10 : 0,
      firstSeen: days[0] ?? "",
      lastSeen: days.at(-1) ?? "",
    }
  }).sort((a, b) => b.total - a.total || a.clusterId - b.clusterId)
}

/** Inclusive calendar days ending at DEMO_END_DATE; absent days are zero-filled. */
export function buildTrend(
  pairs: readonly QAPair[],
  days: DemoDays = 7,
): TrendPoint[] {
  const length = selectedDays(days)
  const points: TrendPoint[] = Array.from({ length }, (_, index) => ({
    date: dayOffset(index + 1 - length),
    answered: 0,
    unanswered: 0,
    total: 0,
  }))
  const byDay = new Map(points.map((point) => [point.date, point]))
  for (const pair of pairs) {
    const point = byDay.get(dayOf(pair))
    if (!point) continue
    point.total += 1
    point[hasAnswer(pair) ? "answered" : "unanswered"] += 1
  }
  return points
}
