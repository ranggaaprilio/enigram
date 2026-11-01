import { AppController } from './app.controller';

async function bootstrap(): Promise<void> {
  console.log(AppController.name);
}

bootstrap();
