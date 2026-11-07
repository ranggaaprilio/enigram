import { Canvas } from "./_components/canvas";

export default function BoardPage({ params }: { params: { boardId: string } }) {
  return <Canvas id={params.boardId} />;
}
