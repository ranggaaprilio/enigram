import { Controller, Get, Param } from '@nestjs/common';
import { AppService } from './app.service';

@Controller('users')
export class AppController {
  constructor(private readonly service: AppService) {}

  @Get(':id')
  find(@Param('id') id: string): string {
    return this.service.find(id);
  }
}
