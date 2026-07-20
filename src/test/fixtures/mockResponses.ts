export const mockResponses: Record<string, string[]> = {
  camden: [
    "I get tired really fast when I play with my sister now.",
    "My arms feel weak sometimes, especially in the morning.",
    "I miss playing outside with my friends at school.",
    "Mommy says I have to rest a lot but I don't like it.",
    "I still like drawing and watching my shows when I'm tired.",
    "Sometimes my tummy hurts after the medicine.",
  ],
  carly: [
    "It's been hard keeping up with work while going through treatment.",
    "My wrists hurt the most when I'm typing or lifting my kids.",
    "I try to stay positive, but some days are harder than others.",
    "My husband has been helping more with the kids' morning routine.",
    "I haven't been able to do yoga like I used to because of the pain.",
    "I worry about missing important things at my kids' school.",
  ],
  sofia: [
    "My wrists and elbows are stiff, especially when I wake up.",
    "I had to sit out of dance practice twice this month.",
    "It's frustrating because dance is the thing I love most.",
    "Some days at school I can't hold my pencil very well.",
    "My friends have been really supportive about it.",
    "I take a warm shower in the morning to help loosen up.",
  ],
  jayden: [
    "I want to get back to running the group program as soon as I can.",
    "Some days the fatigue hits me harder than I expect.",
    "I've had to modify my workouts, which has been an adjustment.",
    "My family has been really supportive of my new routine.",
    "I still go for short walks even on my low-energy days.",
    "I'm worried about flare-ups when I push myself too hard.",
  ],
};

export function getMockResponse(caseId: string, turnIndex: number): string {
  const responses = mockResponses[caseId] ?? [
    "I'm not sure how to answer that right now.",
  ];
  return responses[turnIndex % responses.length];
}
