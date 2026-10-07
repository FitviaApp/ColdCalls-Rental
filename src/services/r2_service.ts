import type { Env } from '../middleware';

async function uploadAudio(
  env: Env,
  fileContent: ArrayBuffer | ArrayBufferView | ReadableStream | string | Blob,
  filename: string,
  contentType: string,
): Promise<{ key: string }> {
  const extension = String(filename).includes('.')
    ? String(filename).slice(String(filename).lastIndexOf('.') + 1)
    : 'mp3';
  const uniqueFilename = `${crypto.randomUUID().replace(/-/g, '')}.${extension}`;
  const key = `audios/${uniqueFilename}`;

  await env.AUDIO_R2.put(key, fileContent, { httpMetadata: { contentType } });
  return { key };
}

async function deleteAudio(env: Env, key: string): Promise<boolean> {
  try {
    await env.AUDIO_R2.delete(key);
    return true;
  } catch {
    return false;
  }
}

export const r2Service = {
  uploadAudio,
  deleteAudio,
};

export { uploadAudio, deleteAudio };
